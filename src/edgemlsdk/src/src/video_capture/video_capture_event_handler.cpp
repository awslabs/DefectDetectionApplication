#include <Panorama/videocapture.h>
#include <Panorama/flowcontrol.h>

using namespace Panorama;

HRESULT VideoCaptureEventHandler::Create(IVideoCaptureEventHandler** ppObj, VideoClipsGenerateCallback cb)
{
    HRESULT hr = S_OK;
    CREATE_COM(VideoCaptureEventHandler, ptr);
    ptr->_clips_generated_cb = std::move(cb);
    *ppObj = ptr.Detach();
    return hr;
}

VideoCaptureEventHandler::~VideoCaptureEventHandler()
{
    COM_DTOR_FIN(VideoCaptureEventHandler);
}

void VideoCaptureEventHandler::OnVideoClipsGenerated(IVideoClipCollection* clips, bool successful)
{
    if(_clips_generated_cb != nullptr)
    {
        _clips_generated_cb(clips, successful);
    }
}