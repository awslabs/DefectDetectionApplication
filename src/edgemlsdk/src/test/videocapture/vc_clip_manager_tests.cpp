#include <list>
#include <nlohmann/json.hpp>

#include <Panorama/comptr.h>
#include <Panorama/videocapture.h>
#include <video_capture/video_clip_manager.h>
#include <TestUtils.h>

using namespace Panorama;

class MockVideoPacket : public UnknownImpl<IVideoPacket>
{
public:
    static HRESULT Create(IVideoPacket** ppObj, bool key_frame, int64_t pts, int64_t dts, int64_t duration)
    {
        COM_FACTORY(MockVideoPacket, Initialize(key_frame, pts, dts, duration));
    }

    ~MockVideoPacket()
    {
        COM_DTOR_FIN(MockVideoPacket);
    }

    bool KeyFrame() override
    {
        return _key_frame;
    }

    ComPtr<IBuffer> Data() override
    {
        return nullptr;
    }

    int64_t PTS() override
    {
        return _pts;
    }

    int64_t DTS() override
    {
        return _dts;
    }

    int64_t Duration() override
    {
        return _duration;
    }

private:
    HRESULT Initialize( bool key_frame, int64_t pts, int64_t dts, int64_t duration)
    {
        _key_frame = key_frame;
        _pts = pts;
        _dts = dts;
        _duration = duration;
        return S_OK;
    }

    int64_t _pts, _dts, _duration;
    bool _key_frame;
};

class MockVideoClip : public UnknownImpl<IVideoClip>
{
public:
    static HRESULT Create(IVideoClip** ppObj, int64_t start, int64_t end)
    {
        COM_FACTORY(MockVideoClip, Initialize(start, end));
    }

    ~MockVideoClip()
    {
        COM_DTOR_FIN(MockVideoClip);
    }

    int64_t StartPTS() override 
    {
        return _start;
    }

    int64_t EndPTS() override 
    {
        return _end;
    }

    int64_t Duration() override 
    {
        return _end - _start;
    }

    uint8_t* VideoData() override 
    {
        return nullptr;
    }

    int64_t Size() override 
    {
        return 0;
    }

    HRESULT WriteFrame(IBuffer* data, int64_t pts, int64_t dts) override 
    {
        return E_NOTIMPL;
    }

    HRESULT Finalize() override 
    {
        return E_NOTIMPL;
    }

private:
    HRESULT Initialize(int64_t start, int64_t end)
    {
        _start = start;
        _end = end;
        return S_OK;
    }

    int64_t _start, _end;
};


TEST(VideoCapture, VideoClipManager)
{
    HRESULT hr = S_OK;

    VideoClipManager mgr;

    std::vector<ComPtr<IVideoPacket>> packets;
    // Create some packets, add a key frame every 5 frames
    int32_t pkt_idx = 0;
    for(; pkt_idx < 50; pkt_idx++)
    {
        ComPtr<IVideoPacket> pkt;
        ASSERT_S(MockVideoPacket::Create(pkt.AddressOf(), (pkt_idx % 5) == false, pkt_idx*1000000, pkt_idx*1000000, 1000000));
        packets.push_back(pkt);
    }

    // Case 1:  There are no clips currently being managed.  Should return an empty list
    {
        std::list<ComPtr<IVideoPacket>> packets_list(packets.begin(), packets.end());
        std::list<ComPtr<IVideoPacket>>::const_iterator unhandled_packets;
        ComPtr<IVideoClipCollection> collection;
        ASSERT_S(mgr.GetReusableClips(collection.AddressOf(), &unhandled_packets, packets_list.cbegin(), packets_list.end(), 10));
        ASSERT_TRUE(collection->Count() == 0);
        ASSERT_EQ(unhandled_packets, packets_list.cbegin());
    }

    // Case 2:  Clips were created from this exact set of packets
    // Add some clips. Each clip consists of 10 frames (2 key frames)
    std::vector<ComPtr<IVideoClip>> added_clips;
    int32_t clip_idx = 0;
    for(; clip_idx < 5; clip_idx++)
    {
        ComPtr<IVideoClip> clip;
        int32_t start = packets[clip_idx*10]->PTS();
        int32_t end = packets[(clip_idx+1)*10 - 1]->PTS();
        ASSERT_S(MockVideoClip::Create(clip.AddressOf(), packets[clip_idx*10]->PTS(), packets[(clip_idx+1)*10 - 1]->PTS()));
        ASSERT_S(mgr.AddVideoClip(clip, 10));
        added_clips.push_back(clip);
    }

    // Try to add a clip that is before the end of the currently managed clips
    {
        ComPtr<IVideoClip> clip;
        ASSERT_S(MockVideoClip::Create(clip.AddressOf(), packets[20]->PTS(), packets[29]->PTS()));
        ASSERT_F(mgr.AddVideoClip(clip, 10));
    }

    // Get the reusable clips.  Should match the clips that were added
    {
        std::list<ComPtr<IVideoPacket>> packets_list(packets.begin(), packets.end());
        std::list<ComPtr<IVideoPacket>>::const_iterator unhandled_packets;
        ComPtr<IVideoClipCollection> collection;
        ASSERT_S(mgr.GetReusableClips(collection.AddressOf(), &unhandled_packets, packets_list.cbegin(), packets_list.end(), 10));
        ASSERT_TRUE(collection->Count() == 5);

        for(int32_t idx = 0; idx < collection->Count(); idx++)
        {
            ASSERT_TRUE(collection->GetClip(idx).Ptr() == added_clips[idx].Ptr());
        }

        ASSERT_EQ(unhandled_packets, packets_list.end());
    }

    // Case 3:  Add some more packets and clips.  Reusable clips should match the clips that were added
    {
        for(; pkt_idx < 100; pkt_idx++)
        {
            ComPtr<IVideoPacket> pkt;
            ASSERT_S(MockVideoPacket::Create(pkt.AddressOf(), (pkt_idx % 5) == false, pkt_idx*1000000, pkt_idx*1000000, 1000000));
            packets.push_back(pkt);
        }

        for(; clip_idx < 10; clip_idx++)
        {
            ComPtr<IVideoClip> clip;
            int32_t start = packets[clip_idx*10]->PTS();
            int32_t end = packets[(clip_idx+1)*10 - 1]->PTS();
            ASSERT_S(MockVideoClip::Create(clip.AddressOf(), packets[clip_idx*10]->PTS(), packets[(clip_idx+1)*10 - 1]->PTS()));
            ASSERT_S(mgr.AddVideoClip(clip, 10));
            added_clips.push_back(clip);
        }

        std::list<ComPtr<IVideoPacket>> packets_list(packets.begin(), packets.end());
        std::list<ComPtr<IVideoPacket>>::const_iterator unhandled_packets;
        ComPtr<IVideoClipCollection> collection;
        ASSERT_S(mgr.GetReusableClips(collection.AddressOf(), &unhandled_packets, packets_list.cbegin(), packets_list.end(), 10));
        ASSERT_TRUE(collection->Count() == 10);

        for(int32_t idx = 0; idx < collection->Count(); idx++)
        {
            ASSERT_TRUE(collection->GetClip(idx).Ptr() == added_clips[idx].Ptr());
        }

        ASSERT_EQ(unhandled_packets, packets_list.end());
    }

    // Case 4:  Add some more packets but no additional clips.  Packet iterator should point to the start of the new packets
    {
        for(; pkt_idx < 150; pkt_idx++)
        {
            ComPtr<IVideoPacket> pkt;
            ASSERT_S(MockVideoPacket::Create(pkt.AddressOf(), (pkt_idx % 5) == false, pkt_idx*1000000, pkt_idx*1000000, 1000000));
            packets.push_back(pkt);
        }

        // Should still get the same 10 resuable clips
        std::list<ComPtr<IVideoPacket>> packets_list(packets.begin(), packets.end());
        std::list<ComPtr<IVideoPacket>>::const_iterator unhandled_packets;
        ComPtr<IVideoClipCollection> collection;
        ASSERT_S(mgr.GetReusableClips(collection.AddressOf(), &unhandled_packets, packets_list.cbegin(), packets_list.end(), 10));
        ASSERT_TRUE(collection->Count() == 10);

        for(int32_t idx = 0; idx < collection->Count(); idx++)
        {
            ASSERT_TRUE(collection->GetClip(idx).Ptr() == added_clips[idx].Ptr());
        }

        ASSERT_EQ((*unhandled_packets)->PTS(), 100000000);
    }

    // Case 5:  Add a short clip that ends immediately before a key frame
    {
        ComPtr<IVideoClip> clip;
        int32_t start = packets[clip_idx*10]->PTS();
        int32_t end = packets[(clip_idx+1)*10 - 1]->PTS();
        ASSERT_S(MockVideoClip::Create(clip.AddressOf(), packets[clip_idx*10]->PTS(), packets[clip_idx*10 + 4]->PTS()));
        ASSERT_S(mgr.AddVideoClip(clip, 10));
        added_clips.push_back(clip);

         // Should get 11 resuable clips even though the last one will be short
        std::list<ComPtr<IVideoPacket>> packets_list(packets.begin(), packets.end());
        std::list<ComPtr<IVideoPacket>>::const_iterator unhandled_packets;
        ComPtr<IVideoClipCollection> collection;
        ASSERT_S(mgr.GetReusableClips(collection.AddressOf(), &unhandled_packets, packets_list.cbegin(), packets_list.end(), 10));
        ASSERT_TRUE(collection->Count() == 11);

        for(int32_t idx = 0; idx < collection->Count(); idx++)
        {
            ASSERT_TRUE(collection->GetClip(idx).Ptr() == added_clips[idx].Ptr());
        }

        ASSERT_EQ((*unhandled_packets)->PTS(), 105000000);
    }

    // Case 6:  Add a short clip that ends immediately before a non key frame
    {
        ComPtr<IVideoClip> clip;
        int32_t start = packets[clip_idx*10]->PTS();
        int32_t end = packets[(clip_idx+1)*10 - 1]->PTS();
        ASSERT_S(MockVideoClip::Create(clip.AddressOf(), packets[clip_idx*10 + 5]->PTS(), packets[clip_idx*10 + 10]->PTS()));
        ASSERT_S(mgr.AddVideoClip(clip, 10));

        // Should get 11 resuable clips because the clip that was last added didn't end on a key frame ergo it must be recreated before we have more packets to add
        std::list<ComPtr<IVideoPacket>> packets_list(packets.begin(), packets.end());
        std::list<ComPtr<IVideoPacket>>::const_iterator unhandled_packets;
        ComPtr<IVideoClipCollection> collection;
        ASSERT_S(mgr.GetReusableClips(collection.AddressOf(), &unhandled_packets, packets_list.cbegin(), packets_list.end(), 10));
        ASSERT_TRUE(collection->Count() == 11);

        for(int32_t idx = 0; idx < collection->Count(); idx++)
        {
            ASSERT_TRUE(collection->GetClip(idx).Ptr() == added_clips[idx].Ptr());
        }

        ASSERT_EQ((*unhandled_packets)->PTS(), 105000000);
    }

    // Case 7: Remove packets from the head of the packets list to indicate they have dropped off the buffer
    {
        // Should get 10 resuable clips because the packets associated to the first clip were dropped off
        packets.erase(packets.begin(), packets.begin() + 10);
        std::list<ComPtr<IVideoPacket>> packets_list(packets.begin(), packets.end());
        std::list<ComPtr<IVideoPacket>>::const_iterator unhandled_packets;
        ComPtr<IVideoClipCollection> collection;
        ASSERT_S(mgr.GetReusableClips(collection.AddressOf(), &unhandled_packets, packets_list.cbegin(), packets_list.end(), 10));
        ASSERT_EQ(collection->Count(), 10);
        ASSERT_EQ(collection->GetClip(0)->StartPTS(), 10000000);
        ASSERT_EQ((*unhandled_packets)->PTS(), 105000000);
    }
}