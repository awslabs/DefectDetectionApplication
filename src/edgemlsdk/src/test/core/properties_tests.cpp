#include <stdio.h>
#include <sstream>

#include <gtest/gtest.h>
#include <Panorama/comptr.h>
#include <Panorama/properties.h>
#include <Panorama/eventing.h>

#include <core/property/property_manager.h>

#include <TestUtils.h>

using namespace std;
using namespace Panorama;

bool FloatCloseEnough(float actual, float expected, float epsilon=0.001)
{
    float delta = actual - expected;
    delta = delta < 0 ? delta * -1 : delta;
    return delta < epsilon;
}

TEST(PropertiesTests, StringPropertyTests)
{
    HRESULT hr;
    ComPtr<IStringProperty> prop;
    ASSERT_S(CreateStringProperty(prop.AddressOf(), "id", "test"));
    EXPECT_EQ(prop->Type(), PropertyType::STRING);
    EXPECT_EQ(strcmp("id", prop->ID()), 0);

    EXPECT_EQ(strcmp("test", prop->Get()), 0);

    // Get the initial value
    ComPtr<IBuffer> buffer;
    prop->Buffer(buffer.AddressOf());
    EXPECT_EQ(strcmp("test", buffer->AsString()), 0);

    // subscribe to callback
    AutoResetEvent evt;
    ComPtr<IPropertyEventHandler> eventHandler = prop->OnPropertyChanged([&](IProperty* prop)
    {
        evt.Set();
    });
    ASSERT_TRUE(eventHandler);

    // Change to string of same length
    ASSERT_S(prop->Set("four"));
    ASSERT_EQ(strcmp(prop->Get(), "four"), 0);
    ASSERT_TRUE(evt.WaitFor(0));

    // Change to string of different length
    ASSERT_S(prop->Set("anotherValue"));
    ASSERT_EQ(strcmp(prop->Get(), "anotherValue"), 0);
    ASSERT_TRUE(evt.WaitFor(0));

    ComPtr<IBuffer> buffer2;
    prop->Buffer(buffer2.AddressOf());
    EXPECT_EQ(strcmp("anotherValue", buffer2->AsString()), 0);

    // Setting same value should not invoke property changed
    ASSERT_EQ(prop->Set("anotherValue"), S_FALSE);
    ASSERT_FALSE(prop->IsJson());
    ASSERT_FALSE(evt.WaitFor(0));

    // Set some value that is json, 
    // shouldn't be interpretted as json because it wasn't created as json
    ASSERT_S(prop->Set("{\"var1\":\"hello\"}"));
    ASSERT_FALSE(prop->IsJson());
    ASSERT_TRUE(evt.WaitFor(0));

    // Remove the event handler, events should no longer be triggered.
    prop->RemovePropertyEventHandler(eventHandler);
    ASSERT_S(prop->Set("Something completely different"));
    ASSERT_FALSE(evt.WaitFor(0));
}

TEST(PropertiesTests, JsonPropertyTests)
{
    HRESULT hr;

    {
        ComPtr<IStringProperty> prop;
        ASSERT_F(CreateJsonProperty(prop.AddressOf(), "id", "notJson"));
    }

    {
        ComPtr<IStringProperty> prop = Property::CreateJson("id", "{\"var\": 5}");
        ASSERT_TRUE(prop);
        ASSERT_TRUE(prop->IsJson());
        ASSERT_F(prop->Set("nonJson"));
        ASSERT_S(prop->Set("{\"var\": 7}"));

        AutoResetEvent evt;
        prop->OnPropertyChanged([&](IProperty* property)
        {
            evt.Set();
        });

        ASSERT_S(prop->Set("{\"var\": 7, \"var2\": 8}"));
        ASSERT_TRUE(evt.WaitFor(0));

        // string is different but JSON object is equivalent
        // Should return S_FALSE since the value wasn't changed
        ASSERT_EQ(prop->Set("{\"var2\": 8, \"var\": 7}"), S_FALSE);
        ASSERT_FALSE(evt.WaitFor(0));
    }
}

TEST(PropertiesTests, IntegerPropertyTests)
{
    HRESULT hr;
    ComPtr<IIntegerProperty> prop = Property::Create("intId", 5);
    ASSERT_TRUE(prop);
    EXPECT_EQ(prop->Type(), PropertyType::INT32);
    EXPECT_EQ(strcmp("intId", prop->ID()), 0);

    EXPECT_EQ(prop->Get(), 5);

    // subscribe to callback
    AutoResetEvent evt;
    prop->OnPropertyChanged([&](IProperty* property)
    {
        evt.Set();
    });

    // change the value
    ASSERT_S(prop->Set(42));
    ASSERT_TRUE(evt.WaitFor(0));
    EXPECT_EQ(prop->Get(), 42);

    // Setting same value should not invoke property changed
    ASSERT_EQ(prop->Set(42), S_FALSE);
    ASSERT_FALSE(evt.WaitFor(0));
}

TEST(PropertiesTests, FloatPropertyTests)
{
    HRESULT hr;
    ComPtr<IFloatProperty> prop = Property::Create("floatId", 5.1f);
    ASSERT_TRUE(prop);
    EXPECT_EQ(prop->Type(), PropertyType::FLOAT);
    EXPECT_EQ(strcmp("floatId", prop->ID()), 0);

    EXPECT_EQ(prop->Get(), 5.1f);

    // subscribe to callback
    AutoResetEvent evt;
    prop->OnPropertyChanged([&](IProperty* prop)
    {
        evt.Set();
    });

    // change the value
    ASSERT_S(prop->Set(42.42f));
    ASSERT_TRUE(evt.WaitFor(0));

    EXPECT_EQ(prop->Get(), 42.42f);

    // Setting same value should not invoke property changed
    ASSERT_EQ(prop->Set(42.42f), S_FALSE);
    ASSERT_FALSE(evt.WaitFor(0));
}

TEST(PropertiesTests, BoolPropertyTests)
{
    HRESULT hr;
    ComPtr<IBooleanProperty> prop = Property::Create("boolId", true);
    ASSERT_TRUE(prop);
    EXPECT_EQ(prop->Type(), PropertyType::BOOL);
    EXPECT_EQ(strcmp("boolId", prop->ID()), 0);

    EXPECT_EQ(prop->Get(), true);

    // subscribe to callback
    AutoResetEvent evt;
    prop->OnPropertyChanged([&](IProperty* prop)
    {
        evt.Set();
    });

    // change the value
    ASSERT_S(prop->Set(false));
    ASSERT_TRUE(evt.WaitFor(0));

    EXPECT_EQ(prop->Get(), false);

    // Setting same value should not invoke property changed
    ASSERT_EQ(prop->Set(false), S_FALSE);
    ASSERT_FALSE(evt.WaitFor(0));
}

TEST(PropertiesTests, PropertyManagerTests)
{
    HRESULT hr = S_OK;

    ComPtr<PropertyManager> mgr;
    ASSERT_S(PropertyManager::Create(mgr.AddressOf(), "test"));

    // Set a single property
    ASSERT_S(mgr->SetProperty("var1", 5));

    // Validate correct value is set
    ComPtr<IProperty> var1;
    ASSERT_S(mgr->GetProperty(var1.AddressOf(), "var1"));
    ASSERT_EQ(5, var1.QueryInterface<IIntegerProperty>()->Get());

    // Do a batch change
    nlohmann::json batchChange;
    batchChange["var1"] = 10;
    batchChange["var2"] = 7;
    ASSERT_S(mgr->SetBatchProperty(nullptr, batchChange));

    // Validate correct value is set
    ComPtr<IProperty> var2;
    ASSERT_S(mgr->GetProperty(var2.AddressOf(), "var2"));
    ASSERT_EQ(10, var1.QueryInterface<IIntegerProperty>()->Get());
    ASSERT_EQ(7, var2.QueryInterface<IIntegerProperty>()->Get());

    // Do a batch change but leave one value the same
    batchChange["var1"] = 12;
    batchChange["var2"] = 7;
    ASSERT_S(mgr->SetBatchProperty(nullptr, batchChange));
    ASSERT_FALSE(strcmp(mgr->ToJson().c_str(), "{\"var1\":12,\"var2\":7}"));

    // Remove a property in the batch set
    batchChange.clear();
    batchChange["var2"] = 10;
    ASSERT_S(mgr->SetBatchProperty(nullptr, batchChange));
    ASSERT_FALSE(strcmp(mgr->ToJson().c_str(), "{\"var2\":10}"));

    // "var1" should no longer be in property manager
    ComPtr<IProperty> var3;
    ASSERT_EQ(mgr->GetProperty(var3.AddressOf(), "var1"), E_NOT_FOUND);

    // Add a json object
    batchChange.clear();

    nlohmann::json jObj;
    jObj["type"] = "string";
    jObj["value"] = "hello";
    batchChange["var3"] = jObj;
    ASSERT_S(mgr->SetBatchProperty(nullptr, batchChange));

    ComPtr<IProperty> jsonProperty;
    ASSERT_S(mgr->GetProperty(jsonProperty.AddressOf(), "var3"));
    ASSERT_TRUE(jsonProperty.QueryInterface<IStringProperty>()->IsJson());
    std::string expected = "{\"var3\":{\"type\":\"string\",\"value\":\"hello\"}}";
    ASSERT_EQ(mgr->ToJson().compare(expected), 0);

    // Add a string that looks like json, but isn't actually a json object
    batchChange.clear();
    batchChange["jVar1"] = "[\"1dqnccrh\"]";
    batchChange["jVar2"] = "{\"url\":\"https://192.168.1.19:8080\",\"username\":\"admin\",\"password\":\"abcdefg123456z\"}";
    
    ComPtr<IPropertyCollection> changed_properties;
    ASSERT_S(mgr->SetBatchProperty(changed_properties.AddressOf(), batchChange));
    ASSERT_EQ(changed_properties->Count(), 2);
    changed_properties.Release();

    expected = "{\"jVar1\":\"[\\\"1dqnccrh\\\"]\",\"jVar2\":\"{\\\"url\\\":\\\"https://192.168.1.19:8080\\\",\\\"username\\\":\\\"admin\\\",\\\"password\\\":\\\"abcdefg123456z\\\"}\"}";
    ASSERT_EQ(mgr->ToJson().compare(expected), 0);

    // Test the "parameters" special property is handled 
    jObj.clear();
    jObj["val2"] = "world";
    jObj["val3"] = "foo";

    batchChange.clear();
    batchChange["val1"] = "hello";
    batchChange["parameters"] = jObj;

    // Changed properties should have 3 values
    ASSERT_S(mgr->SetBatchProperty(changed_properties.AddressOf(), batchChange));
    ASSERT_EQ(changed_properties->Count(), 3);
    ASSERT_TRUE(changed_properties->ContainsKey("val1"));
    ASSERT_TRUE(changed_properties->ContainsKey("val2"));
    ASSERT_TRUE(changed_properties->ContainsKey("val3"));
    changed_properties.Release();

    jObj.clear();
    jObj["val3"] = "bar";
    batchChange["parameters"] = std::move(jObj);

    // Remove val2 in the "parameters" method.  Only val2 should be removed
    ASSERT_S(mgr->SetBatchProperty(changed_properties.AddressOf(), batchChange));
    ASSERT_EQ(changed_properties->Count(), 1);
    ASSERT_TRUE(changed_properties->ContainsKey("val3"));

    {
        ComPtr<IProperty> property;
        ASSERT_S(mgr->GetProperty(property.AddressOf(), "val1"));
    }

    {
        ComPtr<IProperty> property;
        ASSERT_S(mgr->GetProperty(property.AddressOf(), "val3"));
    }
    
    {
        ComPtr<IProperty> property;
        ASSERT_F(mgr->GetProperty(property.AddressOf(), "val2"));
    }
}

HRESULT CliTestCase(IPropertyDelegate** ppObj, std::string inputString)
{
    HRESULT hr = S_OK;

    CommandLineArgs args = CreateCommandLineArgs(inputString);
    ComPtr<IPropertyDelegate> cliDelegate;
    hr = CreateCLIPropertyDelegate(cliDelegate.AddressOf(), args.Count(), args.Values());
    if(FAILED(hr))
    {
        return hr;
    }

    *ppObj = cliDelegate.Detach();
    return hr;
}

TEST(CoreTests, CommandLinePropertiesTests)
{
    HRESULT hr = S_OK;
    {
        ComPtr<IPropertyDelegate> delegate;
        ASSERT_S(CliTestCase(delegate.AddressOf(), "program --testInteger 123"));

        {
            ComPtr<IProperty> prop;
            ASSERT_S(delegate->GetProperty(prop.AddressOf(), "testInteger"));

            ComPtr<IIntegerProperty> int_prop = prop.QueryInterface<IIntegerProperty>();
            ASSERT_TRUE(int_prop != nullptr);
            ASSERT_EQ(int_prop->Get(), 123);
        }
    }

    {
        ComPtr<IPropertyDelegate> delegate;
        ASSERT_S(CliTestCase(delegate.AddressOf(), "program --testFloat 123.123"));

        {
            ComPtr<IProperty> prop;
            ASSERT_S(delegate->GetProperty(prop.AddressOf(), "testFloat"));

            ComPtr<IFloatProperty> float_prop = prop.QueryInterface<IFloatProperty>();
            ASSERT_TRUE(float_prop != nullptr);
            ASSERT_TRUE(FloatCloseEnough(float_prop->Get(), 123.123));
        }
    }

    {
        ComPtr<IPropertyDelegate> delegate;
        ASSERT_S(CliTestCase(delegate.AddressOf(), "program --testBoolean"));

        {
            ComPtr<IProperty> prop;
            ASSERT_S(delegate->GetProperty(prop.AddressOf(), "testBoolean"));

            ComPtr<IBooleanProperty> bool_prop = prop.QueryInterface<IBooleanProperty>();
            ASSERT_TRUE(bool_prop != nullptr);
            ASSERT_TRUE(bool_prop->Get());
        }
    }

    {
        ComPtr<IPropertyDelegate> delegate;
        ASSERT_S(CliTestCase(delegate.AddressOf(), "program --testString some string"));

        {
            ComPtr<IProperty> prop;
            ASSERT_S(delegate->GetProperty(prop.AddressOf(), "testString"));

            ComPtr<IStringProperty> string_prop = prop.QueryInterface<IStringProperty>();
            ASSERT_TRUE(string_prop != nullptr);
            ASSERT_EQ(strcmp(string_prop->Get(), "some string"), 0);
        }
    }

    {
        ComPtr<IPropertyDelegate> delegate;
        ASSERT_S(CliTestCase(delegate.AddressOf(), "program --testInteger 123 --testBoolean --testFloat 123.456 --testString some string"));

        {
            ComPtr<IProperty> prop;
            ASSERT_S(delegate->GetProperty(prop.AddressOf(), "testInteger"));

            ComPtr<IIntegerProperty> int_prop = prop.QueryInterface<IIntegerProperty>();
            ASSERT_TRUE(int_prop != nullptr);
            ASSERT_EQ(int_prop->Get(), 123);
        }

        {
            ComPtr<IProperty> prop;
            ASSERT_S(delegate->GetProperty(prop.AddressOf(), "testFloat"));

            ComPtr<IFloatProperty> float_prop = prop.QueryInterface<IFloatProperty>();
            ASSERT_TRUE(float_prop != nullptr);
            ASSERT_TRUE(FloatCloseEnough(float_prop->Get(), 123.456));
        }

        {
            ComPtr<IProperty> prop;
            ASSERT_S(delegate->GetProperty(prop.AddressOf(), "testBoolean"));

            ComPtr<IBooleanProperty> bool_prop = prop.QueryInterface<IBooleanProperty>();
            ASSERT_TRUE(bool_prop != nullptr);
            ASSERT_TRUE(bool_prop->Get());
        }

        {
            ComPtr<IProperty> prop;
            ASSERT_S(delegate->GetProperty(prop.AddressOf(), "testString"));

            ComPtr<IStringProperty> string_prop = prop.QueryInterface<IStringProperty>();
            ASSERT_TRUE(string_prop != nullptr);
            ASSERT_EQ(strcmp(string_prop->Get(), "some string"), 0);
        }
    }

    {
        ComPtr<IPropertyDelegate> delegate;
        ASSERT_F(CliTestCase(delegate.AddressOf(), "program testInteger 123 --testBoolean --testFloat 123.456 --testString some string"));
    }

    {
        ComPtr<IPropertyDelegate> delegate;
        ASSERT_F(CliTestCase(delegate.AddressOf(), "program testInteger 123"));
    }
}

TEST(CoreTests, StringExpansionTest)
{
    HRESULT hr = S_OK;
    
    ComPtr<IStringProperty> property;
    ASSERT_S(CreateStringProperty(property.AddressOf(), "id", "{\"type\":\"string\", \"value\":\"hello world\"}"));

    ComPtr<IVariableExpansion> variable;
    ASSERT_S(CreateStringExpansion(variable.AddressOf(), property));

    {
        ComPtr<IBuffer> expansionBuffer;
        ASSERT_S(variable->Expand(expansionBuffer.AddressOf()));
        std::string expansion = expansionBuffer->AsString();
        ASSERT_EQ(expansion, "hello world");
        ASSERT_FALSE(variable->Immutable());
    }

    {
        // Change the property.  Stale should be true and expansion should provide new value
        property->Set("{\"type\":\"string\", \"value\":\"hello world 2\"}");
        ComPtr<IBuffer> expansionBuffer;
        ASSERT_TRUE(variable->Stale());
        ASSERT_S(variable->Expand(expansionBuffer.AddressOf()));
        std::string expansion = expansionBuffer->AsString();
        ASSERT_EQ(expansion, "hello world 2");
        ASSERT_FALSE(variable->Immutable());
    }

    {
        // Update the property with the same value
        // Stale should be false
        property->Set("{\"type\":\"string\", \"value\":\"hello world 2\"}");
        ComPtr<IBuffer> expansionBuffer;
        ASSERT_FALSE(variable->Stale());
    }
}

TEST(CoreTests, FilePropertyDelegateTest)
{
    HRESULT hr = S_OK;
    std::string valid = "{\"val1\":2,\"val2\":\"hello\"}";
    std::string invalid = "some non json data";

    FILE* fptr = fopen("valid.txt", "w");
    fwrite(valid.c_str(), valid.length(), sizeof(char), fptr);
    fclose(fptr);

    fptr = fopen("invalid.txt", "w");
    fwrite(invalid.c_str(), invalid.length(), sizeof(char), fptr);
    fclose(fptr);

    ComPtr<IPropertyDelegate> delegate = Property::CreateFilePropertyDelegate("does_not_exist.txt");
    ASSERT_TRUE(delegate == nullptr);

    delegate = Property::CreateFilePropertyDelegate("invalid.txt");
    ASSERT_TRUE(delegate == nullptr);

    delegate = Property::CreateFilePropertyDelegate("valid.txt");
    ASSERT_TRUE(delegate != nullptr);

    ComPtr<IProperty> property1;
    ASSERT_S(delegate->GetProperty(property1.AddressOf(), "val1"));
    ASSERT_EQ(property1.QueryInterface<IIntegerProperty>()->Get(), 2);

    ComPtr<IProperty> property2;
    delegate->GetProperty(property2.AddressOf(), "val2");
    ASSERT_FALSE(strcmp(property2.QueryInterface<IStringProperty>()->Get(), "hello"));

    valid = "{\"val1\":2,\"val2\":\"hello2\"}";
    fptr = fopen("valid.txt", "w");
    fwrite(valid.c_str(), valid.length(), sizeof(char), fptr);
    fclose(fptr);

    ComPtr<IPropertyCollection> changed_properties;
    ASSERT_S(delegate->Synchronize(changed_properties.AddressOf()));
    ASSERT_EQ(changed_properties->Count(), 1);
    ASSERT_TRUE(changed_properties->ContainsKey("val2"));
    ASSERT_FALSE(changed_properties->ContainsKey("val1"));
    
    ComPtr<IProperty> changed_property;
    ASSERT_S(changed_properties->At(changed_property.AddressOf(), 0));
    ASSERT_EQ(changed_property.Ptr(), property2.Ptr());

    ASSERT_FALSE(strcmp(property2.QueryInterface<IStringProperty>()->Get(), "hello2"));
}