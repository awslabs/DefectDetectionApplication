#ifndef __PANORAMA_GST_H__
#define __PANORAMA_GST_H__

#include <gst/gst.h>

#include <Panorama/apidefs.h>
#include <Panorama/comobj.h>
#include <Panorama/flowcontrol.h>
#include <Panorama/properties.h>
#include <Panorama/app.h>
#include <Panorama/message_broker.h>

#define RETRY_MODE_DEFAULT RetryMode::Linear
#define MIN_DEFAULT 10
#define MAX_DEFAULT 300000
#define LINEAR_INCREMENT_DEFAULT 2500
#define EXPO_INCREMENT_DEFAULT 1.7
#define ALL -1
#define MESSAGE_TYPE_DEFAULT GST_MESSAGE_ERROR
#define ERROR_DOMAIN_DEFAULT ErrorDomain::CORE
#define ERROR_CODE_DEFAULT GST_CORE_ERROR_FAILED

namespace Panorama
{
    enum class TypeOfConfigChange
    {
        PipelineContentChanged = 0,
        PipelineAdded,
        PipelineRemoved
    };

    enum class RetryMode
    {
        Linear = 0,
        Exponential
    };
    
    enum class StatusCode
    {
        INITIALIZED = 0, 
        RUNNING,
        SUSPENDED,
        STOPPED,
        EOS,
        ERROR
    };

    enum class ErrorDomain
    {
        CORE = 0,
        LIBRARY,
        RESOURCE,
        STREAM,
        NOT_DEFINED,
        UNKNOWN
    };

    DEF_INTERFACE(IGstElement, "{958BAC5D-7AC4-4151-AFCE-F9E3D97CAEB0}", IUnknownAlias)
    {
        virtual GstElement* Element() = 0;
        operator GstElement*()
        {
            return Element();
        }

        int GObjectRefCount()
        {
            return G_OBJECT(Element())->ref_count;
        }
    };

    /// @brief Object containing information about an error that occured in the pipeline
    DEF_INTERFACE(IPipelineError, "{0D044C75-34F7-4DFC-844E-91ABBDF7EE89}", IUnknownAlias)
    {
        /// @brief Gets the error message
        virtual const char* ErrorMessage() = 0;

        /// @brief Gets a more verbose description of the error
        virtual const char* DebugInfo() = 0;

        /// @brief The message type put onto the message bus given by GStreamer.
        /// Pipeline Error will only happen on these message types
        /// GST_MESSAGE_EOS               = (1 << 0),
        /// GST_MESSAGE_ERROR             = (1 << 1),
        /// GST_MESSAGE_WARNING           = (1 << 2)
        virtual int32_t MessageType() = 0;

        /// @brief Domain of the error.  Domain is defined by a quark, which is an integer
        /// representation of a string in GStreamer.  The values for a quark for a string
        /// can vary from one run to the next.  Domain Quark is mapped to ErrorDomain enum
        /// for ease of use.
        virtual ErrorDomain Domain() = 0;

        /// @brief The orignal quark for the domain
        /// Common values for Domain and Code found here
        /// https://gstreamer.freedesktop.org/documentation/gstreamer/gsterror.html?gi-language=c
        virtual uint32_t DomainQuark() = 0;

        /// @brief Gets the domain (GQuark) as a string
        virtual const char* DomainAsString() = 0;

        /// @brief Code of the error
        virtual int32_t Code() = 0;

        /// @brief 
        /// @return The value of the name property of the element that generated the error
        virtual const char* ElementName() = 0;

        /// @brief 
        /// @return The name of the factory for the element
        virtual const char* ElementFactory() = 0;

        /// @brief
        /// @return The error serialized to a string
        virtual const char* ToString() = 0;
    };

    /// @brief Object containing list of PipelineError 
    DEF_INTERFACE(IPipelineErrorCollection, "{736D01CE-6120-4F22-970A-B3C7FEA5F61A}", IUnknownAlias)
    {
        /// @brief Number of pipeline error objects contained in collection
        virtual int32_t Count() const = 0;

        /// @brief Add PipelineError object to collection
        virtual HRESULT Add(IPipelineError* pObj) = 0;

        /// @brief Remove PipelineError object to collection
        virtual HRESULT Remove(IPipelineError* pObj) = 0;

        /// @brief Get the PipelineError object at specific index
        virtual HRESULT At(IPipelineError** ppObj, int32_t idx) = 0;

        /// @brief Return if the PipelineError object is contained in collection
        virtual bool Contains(IPipelineError* pObj) = 0;
    };

    // TODO: Deprecate
    DEF_INTERFACE(IPipelineDefinition, "{99DC9C10-AECD-4317-9A62-1F0A62CCFF13}", IUnknownAlias)
    {
        virtual const char* Id() = 0;
        virtual const char* GetDefinition() = 0;
    };

    // TODO: Deprecate
    DEF_INTERFACE(IPipelineDefinitionCollection, "{54CAE281-4ECB-4521-8BBB-7665D199EEAC}", IUnknownAlias)
    {
        virtual int32_t Count() const = 0;
        virtual HRESULT Add(IPipelineDefinition* pObj) = 0;
        virtual HRESULT Remove(IPipelineDefinition* pObj) = 0;
        virtual HRESULT At(IPipelineDefinition** ppObj, int32_t idx) = 0;
        virtual bool Contains(IPipelineDefinition* pObj) = 0;
    };

    struct IPipeline;

    /// @brief Interface used by the Pipeline to convey events
    DEF_INTERFACE(IPipelineEventHandler, "{23F8159F-A412-45FB-8EC6-9F43FF77C447}", IUnknownAlias)
    {
        /// @brief Invoked when gstreamer bus receives a message of type GST_MESSAGE_ERROR/GST_MESSAGE_WARNING/GST_MESSAGE_EOS
        /// @param sender The pipeline being monitored by the GStreamer bus
        /// @param error The PipelineError object generated from the message received on the GStreamer bus
        virtual void OnError(IPipeline* sender, IPipelineError* error) = 0;

        /// @brief Invoked when pipeline has a state change
        /// @param sender The pipeline being monitored by the GStreamer bus
        /// @param oldState The gst state of pipeline before state change
        /// @param newState The gst state of pipeline after state change
        virtual void OnStateChanged(IPipeline* sender, int32_t oldState, int32_t newState) = 0;

        /// @brief Invoked when the pipeline event handler is removed from pipeline
        /// @param sender The pipeline from which the event handler is removed
        virtual void OnRemovedFromPipeline(IPipeline* sender) = 0;
    };

    class PipelineEventHandler : public UnknownImpl<IPipelineEventHandler>
    {
    public:
        static HRESULT CreateOnError(IPipelineEventHandler** ppObj, std::function<void(IPipeline*, IPipelineError*)>);
        static HRESULT CreateOnStateChanged(IPipelineEventHandler** ppObj, std::function<void(IPipeline*, int32_t, int32_t)>);

        ~PipelineEventHandler();

        void OnError(IPipeline* sender, IPipelineError* error) override;
        void OnStateChanged(IPipeline* sender, int32_t oldState, int32_t newState) override;
        void OnRemovedFromPipeline(IPipeline* sender) override;

    private:
        std::function<void(IPipeline*, IPipelineError*)> _on_error_cb;
        std::function<void(IPipeline*, int32_t, int32_t)> _on_state_changed_cb;
    };

    /// @brief Interface that wraps a Gstreamer pipeline object
    DEF_INTERFACE(IPipeline, "{5230BF1A-3DF5-4FC1-8291-63383C19BDB6}", IGstElement)
    {
        /// @brief Sets the state on the pipeline
        /// @param desired_state The desired state
        /// @param wait_for_state The state to wait for before returning
        /// @return S_OK on success, error code otherwise
        virtual HRESULT SetState(int32_t desired_state, int32_t wait_for_state) = 0;

        /// @brief Sets the state on the pipeline to GST_STATE_NULL and waits for the pipeline to stop
        /// @return S_OK on success, error code otherwise
        virtual HRESULT Stop() = 0;

        /// @brief Sets the state on the pipeline to GST_STATE_PAUSED and wait for the pipeline to reach that state
        /// @return S_OK on success, error code otherwise
        virtual HRESULT Pause() = 0;

        /// @brief Changes the definition of a pipeline.  If the pipeline is playing it will restart the pipeline
        /// @param definition The new definition
        /// @return S_OK on success, error code otherwise
        virtual HRESULT ChangeDefinition(const char* definition) = 0;

        /// @brief Restarts the pipeline
        /// @return S_OK on success, error code otherwise
        virtual HRESULT Restart() = 0;

        /// @brief If variables were defined in the pipeline definition this will refresh those variables and apply any changes
        /// @return S_OK on success, error code otherwise
        virtual HRESULT Refresh() = 0;

        /// @brief The State of the pipeline.  Returns a GstState
        virtual int32_t State() = 0;

        /// @brief The id of the pipeline
        virtual const char* Id() = 0;

        /// @brief The definition used to create the pipeline
        virtual const char* Definition() = 0;

        /// @brief Adds a pipeline event handler to the pipeline
        /// @param handler The handler that will be invoked for callbacks
        /// @return S_OK on success, error code otherwise
        /// @note This will be refactored to be similar to IMessageBroker/IProtocolClient
        virtual HRESULT AddPipelineEventHandler(IPipelineEventHandler* handler) = 0;

        /// @brief Removes a pipeline event handler from the pipeline
        /// @param handler The handler to remove
        /// @note This will be refactored to be similar to IMessageBroker/IProtocolClient
        virtual void RemovePipelineEventHandler(IPipelineEventHandler* handler) = 0;

        /// @brief Subscribe to errors on the pipeline
        /// @param cb The callback to invoke on pipeline error
        /// @return Pointer to the IPipelineEventHandler that was added
        inline ComPtr<IPipelineEventHandler> OnError(std::function<void(IPipeline*, IPipelineError*)> cb)
        {
            HRESULT hr = S_OK;
            ComPtr<IPipelineEventHandler> handler;
            CHECK_FAIL(PipelineEventHandler::CreateOnError(handler.AddressOf(), std::move(cb)), nullptr);
            CHECK_FAIL(this->AddPipelineEventHandler(handler), nullptr);
            return handler;
        }

        /// @brief Subscribe to pipeline state changles
        /// @param cb The callback to invoke on state change
        /// @return Pointer to the IPipelineEventHandler that was added
        inline ComPtr<IPipelineEventHandler> OnStateChange(std::function<void(IPipeline* sender, int32_t oldState, int32_t newState)> cb)
        {
            HRESULT hr = S_OK;
            ComPtr<IPipelineEventHandler> handler;
            CHECK_FAIL(PipelineEventHandler::CreateOnStateChanged(handler.AddressOf(), std::move(cb)), nullptr);
            CHECK_FAIL(this->AddPipelineEventHandler(handler), nullptr);
            return handler;
        }

        /// @brief Calls SetState on the pipeline with desired state = GST_STATE_PLAYING and wait_for_state = GST_STATE_READY
        /// @return S_OK on success, error code otherwise
        inline HRESULT Start()
        {
            return this->SetState(GST_STATE_PLAYING, GST_STATE_READY);
        }
    };
    
    /// @brief Interface used by the PipelineManager to convey events
    DEF_INTERFACE(IPipelineManagerEventHandler, "{5CB95088-3740-43AB-A043-3BB3A8E4724C}", IUnknownAlias)
    {
        /// @brief Invoked just before a pipeline is to be created by the pipeline manager
        /// @param definition The definition of the pipeline that will be created
        /// @return If you desire that the pipeline manager continues creating the pipeline return FALSE from this method.  
        ///         Returning TRUE will tell the pipeline manager that you will handle the creation of the pipeline 
        virtual bool OnPipelineAddPreview(IPipelineDefinition* definition) = 0;

        /// @brief Invoked just before a pipeline is removed by the pipeline manager
        /// @param pipeline The pipeline to be removed
        /// @return If you desire the pipeline manager to retain a reference to this pipeline return TRUE.
        ///         Returning FALSE will tell the pipeline manager it's ok for the pipeline to be removed
        virtual bool OnPipelineRemovePreview(IPipeline* pipeline) = 0;

        /// @brief Invoked immediately after a pipeline has been created by the pipeline manager
        /// @param pipeline The pipeline that was created
        virtual void OnPipelineAdded(IPipeline* pipeline) = 0;

        /// @brief Invoked once the pipeline manager has removed all references to a pipeline
        /// @param pipelne The pipeline that was removed
        virtual void OnPipelineRemoved(IPipeline* pipelne) = 0;

        /// @brief Invoked when pipeline has a definition change
        /// @param sender The pipeline with definition change
        /// @param newDefinition The new definition that the pipeline will be changed into
        /// @return If you desire that the pipeline restart with new definition return FALSE from this method.  
        ///         Returning TRUE will tell the pipeline that you will handle the definition change
        virtual bool OnDefintionChangePreview(IPipeline* pipeline, const char* newDefinition) = 0;
    };

    class PipelineManagerEventHandler : public UnknownImpl<IPipelineManagerEventHandler>
    {
    public:
        static HRESULT CreatePipelineAddedPreview(IPipelineManagerEventHandler** ppObj, std::function<bool(IPipelineDefinition*)>);
        static HRESULT CreatePipelineAdded(IPipelineManagerEventHandler** ppObj, std::function<void(IPipeline*)>);
        static HRESULT CreatePipelineRemovedPreview(IPipelineManagerEventHandler** ppObj, std::function<bool(IPipeline*)>);
        static HRESULT CreatePipelineRemoved(IPipelineManagerEventHandler** ppObj, std::function<void(IPipeline*)>);
        static HRESULT CreateDefinitionChangedPreview(IPipelineManagerEventHandler** ppObj, std::function<bool(IPipeline*, const char*)>);

        ~PipelineManagerEventHandler();

        bool OnPipelineAddPreview(IPipelineDefinition* definition) override;
        bool OnPipelineRemovePreview(IPipeline* pipeline) override;
        void OnPipelineAdded(IPipeline* pipeline) override;
        void OnPipelineRemoved(IPipeline* pipeline) override;
        bool OnDefintionChangePreview(IPipeline* pipeline, const char* newDefinition) override;

    private:
        std::function<bool(IPipelineDefinition*)> _pipeline_added_preview_cb;
        std::function<void(IPipeline*)> _pipeline_added_cb;
        std::function<bool(IPipeline*)> _pipeline_removed_preview_cb;
        std::function<void(IPipeline*)> _pipeline_removed_cb;
        std::function<bool(IPipeline*, const char*)> _pipeline_definition_change_preview_cb;
    };

    /// @brief Class that handles the lifecycle of multiple pipelines
    DEF_INTERFACE(IPipelineManager, "{0f9419c6-8301-46f3-b3c5-be24c2f57491}", IUnknownAlias)
    {
        /// @brief Initializes the pipeline manager and instantiates all necessary pipelines.  Specified by the 'pipelines' property.
        /// @return S_OK on success, error code otherwise
        virtual HRESULT Initialize() = 0;

        /// @brief Calls SetState(GST_STATE_PLAYING, GST_STATE_READY) on all pipelines
        /// @return S_OK if all pipelines return success, error code otherwise
        virtual HRESULT Start() = 0;

        /// @brief Calls Pipeline::Restart() on all pipelines
        /// @return S_OK if all pipelines return success, error code otherwise
        virtual HRESULT Restart() = 0;

        /// @brief Calls Pipeline::Stop() on all pipelines
        /// @return S_OK if all pipelines return success, error code otherwise
        virtual HRESULT Stop() = 0;

        /// @brief Refreshes the 'pipelines' property used to create the pipelines.  Adds/Removes/Restarts pipelines as necessary
        /// @return S_OK if all pipelines return success, error code otherwise
        virtual HRESULT Refresh() = 0;

        /// @brief Add a pipeline to the manager
        /// @param pipeline_def Definition of the pipeline
        /// @return S_OK on success, error code otherwise
        virtual HRESULT AddPipeline(IPipelineDefinition* pipeline_def) = 0;

        /// @brief Removes a pipelne from the manager
        /// @param id Id of the pipeline
        /// @return S_OK on success, error code otherwise
        virtual HRESULT RemovePipeline(const char* id) = 0;

        /// @brief Update the definition of a specific pipeline
        /// @param pipeline_def The new pipeline definition.  Equivalent to AddPipeline if pipeline id is not currently being managed.
        /// @return S_OK on success, error code otherwise
        virtual HRESULT UpdatePipeline(IPipelineDefinition* pipeline_def) = 0;

        // TODO: This could use a refactor, bit clunky of a design ATM
        virtual HRESULT SetRetryMechanism(IPipelineEventHandler* retry_machanism) = 0;
        virtual HRESULT GetRetryMechanism(IPipelineEventHandler** ppObj) = 0;
        
        /// @brief Gets the number of pipelines currently being managed
        virtual int32_t Count() = 0;

        /// @brief Gets the pipeline with the specific ID
        /// @param ppObj Address of the IPipeline* to point to the pipeline
        /// @param Id Id of the pipeline to retrieve
        /// @return S_OK on success, error code otherwise
        virtual HRESULT GetPipelineById(IPipeline** ppObj, const char* Id) = 0;

        /// @brief Adds a pipeline manager event handler
        /// @param handler The handler that will be invoked for callbacks
        /// @return S_OK on success, error code otherwise
        /// @note This will be refactored to be similar to IMessageBroker/IProtocolClient
        virtual HRESULT AddPipelineManagerEventHandler(IPipelineManagerEventHandler* handler) = 0;

        /// @brief Removes a pipeline manager event handler
        /// @param handler The handler that will be invoked for callbacks
        /// @return S_OK on success, error code otherwise
        /// @note This will be refactored to be similar to IMessageBroker/IProtocolClient
        virtual void RemovePipelineManagerEventHandler(IPipelineManagerEventHandler* handler) = 0;

        /// @brief Subscribe to pipeline added events
        /// @param cb function to invoke when a pipeline is added
        /// @return ComPtr<IPipelineManagerEventHandler> pointer to the handler that was added. nullptr on failure.
        inline ComPtr<IPipelineManagerEventHandler> OnPipelineAdded(std::function<void(IPipeline*)> cb)
        {
            HRESULT hr = S_OK;
            ComPtr<IPipelineManagerEventHandler> handler;
            CHECK_FAIL(PipelineManagerEventHandler::CreatePipelineAdded(handler.AddressOf(), std::move(cb)), nullptr);
            CHECK_FAIL(this->AddPipelineManagerEventHandler(handler), nullptr);
            return handler;
        }
    };

    DLLAPI HRESULT InitializeGStreamerWithArgs(int argc, char* argv[], int gstLogLevel = GST_LEVEL_FIXME);
    DLLAPI HRESULT GstElementMakeCom(IGstElement** ppObj, GstElement* element);
    DLLAPI HRESULT ShutdownGStreamer();
    DLLAPI HRESULT CreatePipelineDefinitionCollection(IPipelineDefinitionCollection** ppObj);
    DLLAPI HRESULT CreatePipelineDefinition(IPipelineDefinition** ppObj, const char* id, const char* definition);
    DLLAPI HRESULT CreatePipeline(IPipeline** ppObj, const char* id, const char* definition, IApp* app);
    DLLAPI HRESULT CreatePipelineManager(IPipelineManager** ppObj, IApp* app);
    DLLAPI HRESULT CreatePipelineError(IPipelineError** ppObj, int32_t msgType, int32_t domain, int32_t code);
    DLLAPI HRESULT CreatePipelineErrorFromMessage(IPipelineError** ppObj, GstMessage* msg);
    DLLAPI HRESULT CreatePipelineErrorCollection(IPipelineErrorCollection** ppObj);
    DLLAPI HRESULT CreateRetryMechanism(IPipelineEventHandler** ppObj, RetryMode retryMode, int32_t minDelayInMs, int32_t maxDelayInMs, float increment, IPipelineErrorCollection* errorCollection);
    DLLAPI HRESULT CreateRetryMechanismDefault(IPipelineEventHandler** ppObj);
    DLLAPI HRESULT CreateRetryMechanismFromProperty(IPipelineEventHandler** ppObj, IStringProperty* configuration);
    DLLAPI HRESULT AddPayloadToBufferMeta(IPayload* payload, GstBuffer* buf, const char* id);
    DLLAPI HRESULT GetPayloadFromBufferMeta(IPayload** ppObj, GstBuffer* buf, const char* id);
    DLLAPI HRESULT SetBufferCorrelationId(GstBuffer* buf, const char* correlation_id);
    DLLAPI HRESULT GetBufferCorrelationId(IBuffer** ppObj, GstBuffer* buf);
    DLLAPI TraceLevel GstLevelToTraceLevel(GstDebugLevel level);
    DLLAPI GstDebugLevel TraceLevelToGstLevel(TraceLevel level);

    class GStreamer
    {
    public:
        /// @brief Initialize the gstreamer framework with arguments
        /// @param argc The number of arguments
        /// @param argv Array of arguments
        /// @param gstLogLevel Log level to set
        /// @return S_OK, if successfully initialized, error code otherwise
        static HRESULT Initialize(int argc, char* argv[], int gstLogLevel = GST_LEVEL_FIXME)
        {
            return InitializeGStreamerWithArgs(argc, argv, gstLogLevel);
        }

        /// @brief Initializes the Gstreamer framework
        /// @param gstLogLevel Log level to set
        /// @return S_OK, if successfully initialized, error code otherwise
        static HRESULT Initialize(int gstLogLevel = GST_LEVEL_FIXME)
        {
            return InitializeGStreamerWithArgs(0, nullptr, gstLogLevel);
        }

        /// @brief Stops the gstreamer framework
        /// @return S_OK, if successfully shutdown, error code otherwise
        static HRESULT Shutdown()
        {
            return ShutdownGStreamer();
        }

        /// @brief Wraps a native GstElement* as a ComPtr
        /// @param element The element to wrap
        /// @return The created ComPtr
        static ComPtr<IGstElement> MakeCom(GstElement* element)
        {
            HRESULT hr = S_OK;
            ComPtr<IGstElement> ptr;
            CHECK_FAIL(GstElementMakeCom(ptr.AddressOf(), element), nullptr);
            return ptr;
        }

        /// @brief Adds a payload to buffer as a PayloadMetaApi
        /// @param payload The payload to add
        /// @param buf The buffer to add the payload too
        /// @param id Identifier for the payload
        /// @return S_OK if successul.  Error code otherwise.
        static HRESULT AddPayloadToBuffer(IPayload* payload, GstBuffer* buf, const char* id)
        {
            return AddPayloadToBufferMeta(payload, buf, id);
        }

        /// @brief Gets a payload from the buffer metadata
        /// @param ppObj Address if object pointing to the payload
        /// @param buf The buffer to retrieve the payload from
        /// @param id Identifier for the payload
        /// @return S_OK if successul.  Error code otherwise.
        static HRESULT GetPayloadFromBuffer(IPayload** ppObj, GstBuffer* buf, const char* id)
        {
            return GetPayloadFromBufferMeta(ppObj, buf, id);
        }
    };

    class Pipeline
    {
    public:
        /// @brief Creates a Pipeline object
        /// @param ppObj Address of the IPipeline Object to hold the created pipeline
        /// @param id Id of the pipeline (ideally should be unique)
        /// @param definition Launch string for the pipeline
        /// @param app Your IApp object
        /// @return S_OK if successful.  Error code otherwise
        static HRESULT Create(IPipeline** ppObj, const char* id, const char* definition, IApp* app)
        {
            return CreatePipeline(ppObj, id, definition, app);
        }
    };


    class PipelineManager
    {
    public:
        /// @brief Creates the pipeline manager
        /// @param ppObj Address of the IPipelineManager object to hold the created manager object
        /// @param app The application object
        /// @return S_OK if successful.  Error code otherwise
        static HRESULT Create(IPipelineManager** ppObj, IApp* app)
        {
            return CreatePipelineManager(ppObj, app);
        }
    };
}
#endif
