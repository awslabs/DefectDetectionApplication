#include <gtest/gtest.h>

#include <misc.h>
#include <TestUtils.h>
#include <filesystem_safe.h>
#include <evictable_list.h>

using namespace std;
using namespace Panorama;

TEST(MiscTests, StringSplit)
{
    std::string input = "some;text;to;split";

    std::vector<std::string> results = SplitString(input, ';');
    ASSERT_EQ(results.size(), 4);
    ASSERT_EQ(results[0].compare("some"), 0);
    ASSERT_EQ(results[1].compare("text"), 0);
    ASSERT_EQ(results[2].compare("to"), 0);
    ASSERT_EQ(results[3].compare("split"), 0);
}

TEST(MiscTests, StringEncapsulation)
{
    std::string input = "some {encapsulated} text to {parse}";

    std::vector<std::string> results = EncapsulatedStrings(input, "\\{", "\\}");
    ASSERT_EQ(results.size(), 2);
    ASSERT_EQ(results[0].compare("encapsulated"), 0);
    ASSERT_EQ(results[1].compare("parse"), 0);
}

TEST(MiscTests, StringFindAndReplace)
{
    std::string input = "some text to replace text all instances";

    FindAndReplace(input, "text", "hello world");
    ASSERT_EQ(input.compare("some hello world to replace hello world all instances"), 0);
}

TEST(MiscTests, MergeJsonTests)
{
    std::string primary = "{\"value\":\"2\"}";
    std::string secondary = "{\"type\":\"string\",\"value\":\"1\"}";
    std::string result = MergeJson(primary, secondary);
    ASSERT_EQ(result.compare("{\"type\":\"string\",\"value\":\"2\"}"), 0);

    primary = "[{\"id\":\"pipeline1\",\"definition\":\"videotestsrc name=src pattern={PATTERN} flip=true ! ximagesink\"}]";
    secondary = "[{\"id\":\"pipeline1\",\"definition\":\"videotestsrc name=src pattern=0 flip=true ! ximagesink\"}]";
    result = MergeJson(primary, secondary);
    ASSERT_EQ(result.compare("[{\"definition\":\"videotestsrc name=src pattern={PATTERN} flip=true ! ximagesink\",\"id\":\"pipeline1\"}]"), 0);
}

TEST(MiscTests, NullifyJsonClutterTest)
{
    // {
    //     "val1": 1,
    //     "val2": 2,
    //     "val3": {
    //         "sub1": 4,
    //         "sub2": 5,
    //         "sub3": 6,
    //         "sub4": {
    //             "subsub1": 7,
    //             "subsub2": 8
    //         },
    //         "sub5": {
    //             "subsub1": 9,
    //             "subsub2": 10
    //         }
    //     },
    //     "val4": {
    //         "sub1": {
    //             "subsub1": {
    //                 "3s1": 1,
    //                 "3s2": 2
    //             }
    //         }
    //     }
    // }
    nlohmann::json cluttered = nlohmann::json::parse("{\"val1\":1,\"val2\":2,\"val3\":{\"sub1\":4,\"sub2\":5,\"sub3\":6,\"sub4\":{\"subsub1\":7,\"subsub2\":8},\"sub5\":{\"subsub1\":9,\"subsub2\":10}},\"val4\":{\"sub1\":{\"subsub1\":{\"3s1\":1,\"3s2\":2}}}}");


    // {
    //     "val1": 1,
    //     "val3": {
    //         "sub1": 4,
    //         "sub3": 6,
    //         "sub4": {
    //             "subsub1": 7,
    //         },
    //     },
    //     "val4": {
    //         "sub1": {
    //             "subsub1": {
    //                 "3s1": 1
    //             }
    //         }
    //     }
    // }
    nlohmann::json clean = nlohmann::json::parse("{\"val1\":1,\"val3\":{\"sub1\":4,\"sub3\":6,\"sub4\":{\"subsub1\":7}},\"val4\":{\"sub1\":{\"subsub1\":{\"3s1\":1}}}}");

    NullifyJsonClutter(clean, cluttered);

    // Expected results
    // {
    //     "val1": 1,
    //     "val2": null,
    //     "val3": {
    //         "sub1": 4,
    //         "sub2": null,
    //         "sub3": 6,
    //         "sub4": {
    //             "subsub1": 7,
    //             "subsub2": null
    //         },
    //         "sub5": null
    //     },
    //     "val4": {
    //         "sub1": {
    //             "subsub1": {
    //                 "3s1": 1,
    //                 "3s2": null
    //             }
    //         }
    //     }
    // }
    ASSERT_EQ(0, strcmp(clean.dump().c_str(), "{\"val1\":1,\"val2\":null,\"val3\":{\"sub1\":4,\"sub2\":null,\"sub3\":6,\"sub4\":{\"subsub1\":7,\"subsub2\":null},\"sub5\":null},\"val4\":{\"sub1\":{\"subsub1\":{\"3s1\":1,\"3s2\":null}}}}"));
}

TEST(MiscTests, LexicallyNormalTest)
{
    EXPECT_EQ(LexicallyNormal("/a/./file.txt"), "/a/file.txt");
    EXPECT_EQ(LexicallyNormal("/a/b/../file.txt"), "/a/file.txt");
    EXPECT_EQ(LexicallyNormal("/"), "/");
    EXPECT_EQ(LexicallyNormal("/a/.////"), "/a/");
    EXPECT_EQ(LexicallyNormal("/a/./b/.."), "/a/");
    EXPECT_EQ(LexicallyNormal("/a/.///b/../"), "/a/");
}

TEST(MiscTests, GuidGeneration)
{
    Guid a = GenerateGuid();
    Guid b = GenerateGuid();

    ASSERT_FALSE(a == b);
}

TEST(MiscTests, GuidToFromString)
{
    Guid g1 = GuidFromString("A0BE4CF1-0241-4157-B7F1-4E5D35D92990");
    Guid g2 = GuidFromString("{A0BE4CF1-0241-4157-B7F1-4E5D35D92990}");
    Guid g3 = GuidFromString("A0BE4CF1-0241-4157-B7F1-4E5D35D92991");

    ASSERT_EQ(g1.Data1, 2696826097);
    ASSERT_EQ(g1.Data2, 577);
    ASSERT_EQ(g1.Data3, 16727);
    ASSERT_EQ(g1.Data4[0], 183);
    ASSERT_EQ(g1.Data4[1], 241);
    ASSERT_EQ(g1.Data4[2], 78);
    ASSERT_EQ(g1.Data4[3], 93);
    ASSERT_EQ(g1.Data4[4], 53);
    ASSERT_EQ(g1.Data4[5], 217);
    ASSERT_EQ(g1.Data4[6], 41);
    ASSERT_EQ(g1.Data4[7], 144);

    ASSERT_EQ(g1, g2);
    ASSERT_NE(g1, g3);

    std::string g1_str = GuidToString(g1);
    ASSERT_TRUE(g1_str.compare("A0BE4CF1-0241-4157-B7F1-4E5D35D92990") == 0);
}

TEST(MiscTests, EvictableListNoAccumulator)
{
    HRESULT hr = S_OK;

    struct Frame
    {
        int32_t FrameType;
        int32_t Timestamp;
    };

    typedef std::list<Frame>::const_iterator iter;

    std::function<const iter(const iter&, const iter&, int32_t)> evict =
        [&](const iter& begin, const iter& end, int32_t size)
        {
            if(end->Timestamp - begin->Timestamp > 250)
            {
                auto iter = begin;
                do
                {
                    iter++;
                } while (iter != end && (iter->FrameType != 0 || end->Timestamp - iter->Timestamp > 250));

                return iter;
            }

            return begin;
        };

    {
        EvictableList<Frame> list(evict);

        // Validate initial state
        ASSERT_EQ(list.Count(), 0);
        std::list<Frame> snapshot = list.Snapshot();
        ASSERT_EQ(snapshot.size(), 0);
        ASSERT_EQ(list.Size(), 0);

        // Validate insertions are succeeding.
        ASSERT_S(list.Insert({0, 0}));
        ASSERT_S(list.Insert({1, 50}));
        ASSERT_S(list.Insert({1, 100}));
        ASSERT_S(list.Insert({1, 150}));
        ASSERT_S(list.Insert({1, 200}));
        ASSERT_S(list.Insert({0, 225}));
        ASSERT_S(list.Insert({1, 275}));
        ASSERT_EQ(list.Count(), 2);

        // Since accumulator wasn't provided Size() should still be 0
        ASSERT_EQ(list.Size(), 0);

        // Validate insertions did not effect snapshot
        ASSERT_EQ(snapshot.size(), 0);

        // Validate evicition policy was correctly applied
        snapshot = list.Snapshot();
        auto head = snapshot.begin();
        ASSERT_EQ(head->FrameType, 0);
        ASSERT_EQ(head->Timestamp, 225);
        head++;
        ASSERT_EQ(head->FrameType, 1);
        ASSERT_EQ(head->Timestamp, 275);
    }

    {
        // No eviction policy, acts like a std::list
        EvictableList<Frame> list(nullptr);
        ASSERT_S(list.Insert({0, 0}));
        ASSERT_S(list.Insert({1, 50}));
        ASSERT_S(list.Insert({1, 100}));
        ASSERT_S(list.Insert({1, 150}));
        ASSERT_S(list.Insert({1, 200}));
        ASSERT_S(list.Insert({0, 225}));
        ASSERT_S(list.Insert({1, 275}));
        ASSERT_EQ(list.Count(), 7); 
    }
}
  
TEST(MiscTests, EvictableListWithAccumulation)
{
    HRESULT hr = S_OK;
    typedef std::list<ComPtr<IBuffer>>::const_iterator iter;

    // Test accumulator callback
    AutoResetEvent accumulator_called;
    std::function<int64_t(const ComPtr<IBuffer>&)> accumulator = 
        [&](const ComPtr<IBuffer>& element)
        {
            accumulator_called.Set();
            return element->Size();
        };

    // Test eviction policty
    AutoResetEvent eviction_policy_called;
    std::function<const iter(const iter&, const iter&, int32_t, int64_t)> evict =
        [&](const iter& begin, const iter& end, int32_t count, int64_t accumulation)
        {
            auto iterator = begin;
            while(accumulation > 2048)
            {
                accumulation -= (*iterator)->Size();
                iterator++;
            }

            eviction_policy_called.Set();
            return iterator;
        };

    {
        EvictableList<ComPtr<IBuffer>, int64_t> list(evict, accumulator, 0);

        // Validate default values
        ASSERT_EQ(list.Size(), 0);
        ASSERT_EQ(list.Count(), 0);

        for(int32_t count = 0; count < 5; count++)
        {
            ComPtr<IBuffer> buffer;
            ASSERT_S(Buffer::Create(buffer.AddressOf(), 1024));
            memset(buffer->Data(), count, buffer->Size());
            ASSERT_S(list.Insert(buffer));
            ASSERT_TRUE(accumulator_called.WaitFor(0));
            ASSERT_TRUE(eviction_policy_called.WaitFor(0));
        }

        // Validate count and size were incremented correctly
        ASSERT_EQ(list.Count(), 2);
        ASSERT_EQ(list.Size(), 2048);

        // Validate evicition policy was correctly applied
        std::list<ComPtr<IBuffer>> snapshot = list.Snapshot();
        auto head = snapshot.begin();
        ASSERT_EQ((*head)->Data()[0], 3);
        head++;
        ASSERT_EQ((*head)->Data()[0], 4);
    }

    {
        // Accumulator not specified
        EvictableList<ComPtr<IBuffer>, int64_t> list(evict, nullptr, 0);

        for(int32_t count = 0; count < 5; count++)
        {
            ComPtr<IBuffer> buffer;
            ASSERT_S(Buffer::Create(buffer.AddressOf(), 1024));
            memset(buffer->Data(), count, buffer->Size());
            ASSERT_S(list.Insert(buffer));
            ASSERT_FALSE(accumulator_called.WaitFor(0));
            ASSERT_TRUE(eviction_policy_called.WaitFor(0));
        }

        // Validate count and size were incremented correctly
        ASSERT_EQ(list.Count(), 5);
        ASSERT_EQ(list.Size(), 0);
    }

    {
        // Accumulator nor eviction specified
        EvictableList<ComPtr<IBuffer>, int64_t> list(nullptr, nullptr, 0);

        for(int32_t count = 0; count < 5; count++)
        {
            ComPtr<IBuffer> buffer;
            ASSERT_S(Buffer::Create(buffer.AddressOf(), 1024));
            memset(buffer->Data(), count, buffer->Size());
            ASSERT_S(list.Insert(buffer));
            ASSERT_FALSE(accumulator_called.WaitFor(0));
            ASSERT_FALSE(eviction_policy_called.WaitFor(0));
        }

        // Validate count and size were incremented correctly
        ASSERT_EQ(list.Count(), 5);
        ASSERT_EQ(list.Size(), 0);
    }
}