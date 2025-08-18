#include <thread>
#include <mutex>
#include <iostream>
#include <list>

#include <glib.h>
#include <gst/gst.h>
#include <nlohmann/json.hpp>

#include <Panorama/comptr.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/gst.h>
#include <Panorama/eventing.h>

#include <scheduling.h>
#include <collection_base.h>

#include "gst_runner.h"


using namespace Panorama;


struct RestartJobContext
{
    ManualResetEvent JobDone;
    std::string Definition;
};

struct PipelineStatus
{
    StatusCode Code;
    std::string Description;
};

class PipelineImpl : public UnknownImpl<IPipeline>
{
public:
    static HRESULT Create(IPipeline** ppObj, const char* id, const char* definition, IApp* app)
    {
        COM_FACTORY(PipelineImpl, Initialize(id, definition, app));
    }

    const char* Id() override
    {
        return _id.c_str();
    }

    const char* Definition() override
    {
        return _definition.c_str();
    }

     ~PipelineImpl()
    {
        COM_DTOR(PipelineImpl);
        if(_structure != nullptr)
        {
            if(FAILED(Stop()))
            {
                TraceError("Failed to stop pipeline");
            }
        }

        COM_DTOR_FIN(PipelineImpl);
    }

    HRESULT SetState(int32_t desired_state, int32_t wait_for_state) override
    {
        _latest_desired_state = desired_state;
        _state_changed.Set();
        std::lock_guard<std::mutex> lk(_stateLock);
        HRESULT hr = S_OK;
        if(_validPipeline == false)
        {
            TraceError("Pipeline [%s]: Invalid pipeline definition, cannot start", this->Id());
            return E_INVALID_STATE;
        }

        CHECKHR(SetState(static_cast<GstState>(desired_state)));
        CHECKIF_MSG(WaitForState(static_cast<GstState>(wait_for_state)) == false, E_INVALID_STATE, "Never transitioned to GST_STATE_PLAYING");
        if (_status.Code != StatusCode::ERROR)
        {
            _status.Code = StatusCode::RUNNING;
            _status.Description = "Pipeline is running";
        }

        TraceInfo("Pipeline [%s]: RUNNING", this->Id());
        return hr;
    }

    HRESULT Stop() override
    {
        _latest_desired_state = GST_STATE_NULL;
        _state_changed.Set();
        std::lock_guard<std::mutex> lk(_stateLock);
        HRESULT hr = S_OK;

        if(_status.Code == StatusCode::STOPPED)
        {
            return S_OK;
        }

        TraceInfo("Pipeline [%s]: STOPPING", this->Id());

        // Need to stop main loop before setting state to null because state change eventing could override state
        // wait for main loop to stop
        if(_loop != nullptr)
        {
            g_main_loop_quit(_loop);
            g_main_loop_unref(_loop);
            _loop = nullptr;
        }

        TraceVerbose("Pipeline [%s]: Waiting for main loop to stop", this->Id());
        if(_gst_loop_thread.joinable())
        {
            _gst_loop_thread.join();
        }

        TraceVerbose("Pipeline [%s]: Main loop has stopped", this->Id());

        // change pipeline state to null
        CHECKHR(SetState(GST_STATE_NULL));

        GstState state;
        GstStateChangeReturn ret = gst_element_get_state(Element(), &state, NULL, GST_CLOCK_TIME_NONE);
        if(ret != GST_STATE_CHANGE_SUCCESS)
        {
            TraceError("Error getting pipeline state");
            return E_FAIL;
        }
        else if(state != GST_STATE_NULL)
        {
            TraceError("Pipeline [%s]: Error changing pipeline state to NULL", this->Id());
            return E_FAIL;
        }

        _status.Code = StatusCode::STOPPED;
        _status.Description = "Pipeline is stopped";

        // unref gst objects
        if(_bus != nullptr)
        {
            gst_object_unref(_bus);
            _bus = nullptr;
        }

        if(_watch_id > 0)
        {
            g_source_remove(_watch_id);
            _watch_id = 0;
        }

        TraceInfo("Pipeline [%s]: Stopped. Current state is %d", this->Id(), this->State());
        _structure->DynamicProperties.clear();
        return hr;
    }

    HRESULT Pause() override
    {
        _latest_desired_state = GST_STATE_PAUSED;
        _state_changed.Set();
        std::lock_guard<std::mutex> lk(_stateLock);
        HRESULT hr = S_OK;
        // change pipeline state to paused
        CHECKHR(SetState(GST_STATE_PAUSED));
        CHECKIF(WaitForState(GST_STATE_PAUSED) == false, E_INVALID_STATE);

        if (_status.Code != StatusCode::ERROR)
        {
            _status.Code = StatusCode::SUSPENDED;
            _status.Description = "Pipeline is suspended";
        }
        return hr;
    }

    HRESULT Restart() override
    {
        return ChangeDefinition(Definition());
    }

    HRESULT ChangeDefinition(const char* definition) override
    {
        return RestartInternal(definition);
    }

    HRESULT AddPipelineEventHandler(IPipelineEventHandler* handler) override
    {
        CHECKNULL(handler, E_INVALIDARG);
        std::lock_guard<std::mutex> lk(_handlerLock);
        _pipeline_event_handler.push_back(handler);
        return S_OK;
    }

    void RemovePipelineEventHandler(IPipelineEventHandler* handler) override
    {
        std::lock_guard<std::mutex> lk(_handlerLock);
        if(handler != nullptr)
        {
            auto it = std::find(_pipeline_event_handler.begin(), _pipeline_event_handler.end(), handler);
            if(it != _pipeline_event_handler.end())
            {
                _pipeline_event_handler.erase(it);
                handler->OnRemovedFromPipeline(this);
            }
        }
    }

    GstElement* Element() override
    {
        return _structure->Pipeline->Element();
    } 

    int32_t State() override
    {
        if(Element() != nullptr)
        {
            GstState state;
            // do not wait for async state change to complete, return immediately with current state
            GstStateChangeReturn ret = gst_element_get_state(Element(), &state, NULL, 0);
            return static_cast<int32_t>(state);
        }

        return static_cast<int32_t>(GST_STATE_NULL);
    }

    HRESULT Refresh() override
    {
        HRESULT hr = S_OK;

        TraceInfo("Pipeline [%s]: Refreshing", this->Id());

        // Loop through each dynamic property and see if it's stale.
        for(int32_t idx = 0; idx < _structure->DynamicProperties.size(); idx++)
        {
            DynamicProperty dynProperty = _structure->DynamicProperties[idx];
            if(dynProperty.Variable->Stale())
            {
                TraceInfo("Pipeline [%s]: Property %s is stale, refreshing", this->Id(), dynProperty.Variable->Id());

                // An immutable property has become stale, need to restart pipeline
                if(dynProperty.Variable->Immutable())
                {
                    TraceInfo("Pipeline [%s]: Property %s is immutable, restarting", this->Id(), dynProperty.Variable->Id());
                    return this->Restart();
                }

                // Expand the stale property and set the Gst property
                ComPtr<IBuffer> expansion_buffer;
                CHECKHR(dynProperty.Variable->Expand(expansion_buffer.AddressOf()));
                std::string expansion = expansion_buffer->AsString();
                try
                {
                    int32_t val = std::stoi(expansion);
                    g_object_set(G_OBJECT(dynProperty.Element->Element()), dynProperty.PropertyName.c_str(), val, NULL);
                    continue;
                }
                catch(...)
                {
                }

                try
                {
                    float val = std::stof(expansion);
                    g_object_set(G_OBJECT(dynProperty.Element->Element()), dynProperty.PropertyName.c_str(), val, NULL);
                    continue;
                }
                catch(...)
                {
                }

                g_object_set(G_OBJECT(dynProperty.Element->Element()), dynProperty.PropertyName.c_str(), expansion.c_str(), NULL);
            }
        }

        return hr;
    }

private:
    PipelineImpl() = default;

    HRESULT Initialize(const char* id, const char* definition, IApp* app)
    {
        HRESULT hr = S_OK;
        CHECKNULL(id, E_INVALIDARG);
        CHECKNULL(definition, E_INVALIDARG);
        CHECKNULL(app, E_INVALIDARG);

        _id = id;
        _definition = definition;
        _app = app;

        // Create the initial pipeline
        CHECKHR(BuildPipeline(this->Definition()));
        CHECKHR(AddBusWatch());
        return hr;
    }

    bool WaitForState(GstState state)
    {
        TraceVerbose("Waiting for state %s", gst_element_state_get_name(state));

        // Wait until we transition to the desired state until:
        // - Reqeusted state is reached
        // - Pipeline is an error state
        // - The desired state is no longer the state we are waiting on
        bool reached_state = this->State() == state;
        while(reached_state == false && _status.Code != StatusCode::ERROR && state == _latest_desired_state)
        {
            _state_changed.Wait();
            reached_state = this->State() == state;
        }

        return reached_state;
    }

    HRESULT AddBusWatch()
    {
        // Monitor the gst bus and add callback function
        _bus = gst_pipeline_get_bus(GST_PIPELINE(_structure->Pipeline->Element()));
        CHECKNULL_MSG(_bus, E_FAIL, "Cannot get the gst bus of pipeline");

        _watch_id = gst_bus_add_watch(_bus, GstErrorCallback, this);
        CHECKIF_MSG(_watch_id == 0, E_FAIL, "Cannot add bus watch for pipeline");

        TraceVerbose("Pipeline [%s].  Adding gst main loop", this->Id());
        _gst_loop_thread = std::thread([&]() 
        { 
            _loop = g_main_loop_new(NULL, FALSE);
            g_main_loop_run(_loop);
        });

        // Since loop is started in a background thread it's possible for g_main_loop_run to be called
        // after g_main_loop_quit is called in Stop.  Wait for loop to be considered running before 
        // returning from this method to avoid this race condition
        while(_loop == nullptr || g_main_loop_is_running(_loop) == false)
        {
            ThreadSleep(1);
        }

        TraceVerbose("Pipeline [%s].  Main loop is running", this->Id());
        return S_OK;
    }

    HRESULT RestartInternal(std::string definition)
    {
        HRESULT hr = S_OK;
        CHECKHR_MSG(Stop(), "Fail to stop pipeline");
        CHECKHR_MSG(BuildPipeline(definition.c_str()), "Fail to set pipeline definition");
        CHECKHR_MSG(AddBusWatch(), "Fail to add bus watch to pipeline");
        CHECKHR_MSG(Start(), "Fail to start pipeline");
        return hr;
    }

    HRESULT BuildPipeline(const char* definition)
    {
        HRESULT hr = S_OK;

        TraceInfo("Pipeline [%s] setting definition to %s", this->Id(), definition);

        // Get the pipeline + dynamic variables from the definition
        std::shared_ptr newStructure = std::make_shared<PipelineStructure>();
        hr = GetPipelineStructure(newStructure.get(), definition, _app);
        if(FAILED(hr))
        {
            _status.Code = StatusCode::ERROR;
            _status.Description = "Pipeline with invalid definition cannot be initialized";
            _validPipeline = false;
            return hr;
        }

        // Update structure and contexts with new values
        _structure = std::move(newStructure);
        gst_object_set_name(GST_OBJECT(_structure->Pipeline->Element()), this->Id()); 

        _definition = definition;
        _status.Code = StatusCode::INITIALIZED;
        _status.Description = "Pipeline is initialized and ready to play";
        _validPipeline = true;
        return hr;
    }

    static gboolean GstErrorCallback(GstBus *bus, GstMessage *msg, void* data) 
    {
        PipelineImpl* pipeline = reinterpret_cast<PipelineImpl*>(data);

        // Pipeline is stopping, ignore bus events
        if(pipeline->_loop == nullptr)
        {
            return true;
        }

        switch (GST_MESSAGE_TYPE(msg)) {
            case GST_MESSAGE_ERROR:
            case GST_MESSAGE_WARNING:
            case GST_MESSAGE_EOS:
            {
                ComPtr<IPipelineError> pipelineError;
                CHECKIF(FAILED(CreatePipelineErrorFromMessage(pipelineError.AddressOf(), msg)), TRUE);

                if(GST_MESSAGE_TYPE(msg) == GST_MESSAGE_ERROR || GST_MESSAGE_TYPE(msg) == GST_MESSAGE_EOS)
                {
                    pipeline->_status.Code = GST_MESSAGE_TYPE(msg) == GST_MESSAGE_ERROR ? StatusCode::ERROR : StatusCode::EOS;
                    pipeline->_status.Description = pipelineError->ErrorMessage();
                }

                TraceError("Pipeline [%s]: %s", pipeline->Id(), pipelineError->ToString());
                auto eventHandlers(pipeline->_pipeline_event_handler);
                for(const auto& elem : eventHandlers)
                {
                    elem->OnError(pipeline, pipelineError);
                }

                // indicate error status to the invoking thread
                // to wake up the WaitForState call
                pipeline->_state_changed.Set();
                break;
            }
            case GST_MESSAGE_STATE_CHANGED:
            {
                GstObject* msg_src = GST_OBJECT(GST_MESSAGE_SRC(msg));
                const char* msg_src_type = g_type_name(G_OBJECT_TYPE(msg_src));
                if(strcmp(msg_src_type, "GstPipeline") == 0)
                {
                    /* state changed */
                    GstState old_state, new_state;
                    gst_message_parse_state_changed(msg, &old_state, &new_state, NULL);
                    TraceVerbose("Pipeline [%s]: Changing from state %s to %s", pipeline->Id(), gst_element_state_get_name(old_state), gst_element_state_get_name(new_state));
                    pipeline->_state_changed.Set();

                    auto eventHandlers(pipeline->_pipeline_event_handler);
                    for(const auto& elem : eventHandlers)
                    {
                        elem->OnStateChanged(pipeline, static_cast<int32_t>(old_state), static_cast<int32_t>(new_state));
                    }
                }
                break;
            }
            default:
                /* other message */
                break;
        }
        return TRUE;
    }

    HRESULT SetState(GstState state)
    {
        if(_structure == nullptr || _structure->Pipeline == nullptr || _structure->Pipeline->Element() == nullptr)
        {
            return S_FALSE;
        }

        TraceVerbose("Pipeline [%s]: Setting state to %s", this->Id(), gst_element_state_get_name(state));
        GstStateChangeReturn state_result = gst_element_set_state(_structure->Pipeline->Element(), state);
        if(state_result == GST_STATE_CHANGE_FAILURE)
        {
            TraceError("Error changing state of pipeline to '%s'", gst_element_state_get_name(state));
            return E_FAIL;
        }

        return S_OK;
    }

    ComPtr<IApp> _app;

    // Gstreamer Control
    std::thread _gst_loop_thread; 
    GMainLoop* _loop;
    GstBus* _bus;
    uint32_t _watch_id;
    AutoResetEvent _state_changed;
    bool _validPipeline;

    // Pipeline and Dynamic Variables
    std::string _definition;
    std::string _id;
    PipelineStatus _status;
    std::shared_ptr<PipelineStructure> _structure;
    int32_t _latest_desired_state;

    std::mutex _stateLock;
    std::mutex _handlerLock;

    // Events
    std::list<ComPtr<IPipelineEventHandler>> _pipeline_event_handler;
};

DLLAPI HRESULT CreatePipeline(IPipeline** ppObj, const char* id, const char* definition, IApp* app)
{
    return PipelineImpl::Create(ppObj, id, definition, app);
}