.. |br| raw:: html

   <br />

.. _properties:

==========
Properties
==========

The Panorama-SDK provides the Property interface to generically define a property, all of which are backed by the Buffer(doc TODO) interface.  This SDK provides 4 property types

- Integer
- Float
- Boolean
- String

The Panorama-SDK offers several ways to retrieve configuration parameters (properties) for your application, along with the option to customize these methods. Every configuration parameter, regardless of its source (such as command line, IoT, S3, etc.), is accessed through the PropertyDelegate interface. This interface ensures a consistent API for fetching a property based on its unique identifier. However, it's worth noting that two different PropertyDelegates might yield different properties even if they share the same identifier.  The PropertyDelegates provided by this SDK are:

-   **Command line** |br|
    This reads properties specified as command line inputs to your program.  The general form is as follows

.. code-block:: bash

    #!/bin/bash
    your_application --property_name property_value

    Where "property_name" is the unique identifier you wish to assign to your property and "property_value" is the value you wish to assign to that property.  The SDK will automatically determine if the property is a string, integer, or float.  If you wish to set a boolean property you simply need only specify the property_name.

.. code-block:: bash

    #!/bin/bash
    your_application --my_boolean_property

-   **MDS** |br|
    This retrieves properties that are specified in tge application manifiest used when deploying the application onto the Panorama appliance.  For more information on the application graph see https://docs.aws.amazon.com/panorama/latest/dev/applications-manifest.html

-   **S3** |br|
    An S3 artifact that contains a JSON document that defines each property and their values.

-   **File** |br|
    A file that contains a JSON document that defines each property and their values.
    
    An example JSON document for S3 and File property delegates:

.. code-block:: json

    {
        "my_property1": "some_value",
        "my_property2": 5,
        "my_property3": 15.0,
        "my_property4": True,
    }

-   **IoT** |br|
    TODO

Application PropertyDelegate
----------------------------

The App interface, which extends the PropertyDelegate, facilitates the amalgamation of multiple PropertyDelegates. This design ensures that applications can interact with a singular PropertyDelegate while fetching properties from a variety of sources.  The App object will always instantiate the Command Line PropertyDelegate.  If running on the Panorama appliance the App object will also instantiate the MDS PropertyDelegate.  In order to specify the remaining PropertyDelegates that should be instatiated the application developer should specify either the "PropertyDelegates" or "PropertyDelegatesFile" property on the command line or in the appplication manifest (if running on the Panorama appliance).

-   **PropertyDelegates** |br|
    A string property that contains the JSON string that defines all additional property delegates to be instantiated.

-   **PropertyDelegatesFile** |br|
    A string property that contains the path to a JSON document that defines all additional property delegates to be instantiated.

The structure of the the JSON object that defines the additional property delegates is as follows:

.. code-block:: json

    [
        {
            "type": "file",
            "path": "absolute or relative path to document"
        },
        {
            "type": "s3",
            "bucket": "bucket name",
            "key": "key name",
            "region": "aws region"  
        },
        {
            "type": "iot",
            "thingName": "name of thing",
            "shadowName": "name of shadow (optional, uses classic if not specified)"
            "region": "aws region"
        }
    ]

Example
^^^^^^^

In this example we will create a file that specifies the additional PropertyDelegates to be instantiated, in this case another file which contains a set of properties.

..  code-block:: json
    :caption: delegates.json
    
    [
        {
            "type": "file",
            "path": "./config.json"
        }
    ]

..  code-block:: json
    :caption: config.json
    
    {
        "property1": "Hello World",
        "property2": 5
    }

..  code-block:: bash

    your_application --property0 "command line input" --PropertyDelegatesFile ./delegates.json

..  tabs::

    ..  group-tab:: C++

        ..  code-block:: cpp

            int main(int argc, char* argv[])
            {
                ComPtr<IApp> app = App::CreateWithArgs(argc, argv);

                ComPtr<IStringProperty> property0, property1;
                ComPtr<IIntegerProperty> property2;

                CHECKHR(app->GetProperty(property0.AddressOf(), "property0"));
                CHECKHR(app->GetProperty(property1.AddressOf(), "property1"));
                CHECKHR(app->GetProperty(property2.AddressOf(), "property2"));

                TraceInfo("'%s' '%s' '%d'", property0->Get(), property1->Get(), property2->Get());
            }

    ..  group-tab:: Python

        ..  code-block:: python

            import sys
            from panorama import app
            from panorama import properties
            from panorama import trace

            def main():
                args = sys.argv[1:]
                byte_literals = [arg.encode() for arg in args]
                app = application.create(byte_literals)

                property0 = app.get_property("property0")
                property1 = app.get_property("property1")
                property2 = app.get_property("property2")

                trace.info(f"'{property0.get_value()}' '{property1.get_value()}' '{property2.get_value()}'")
                

            if __name__ == "__main__":
                main()

Output from running the application with the specified inputs would be

..  code-block:: bash

    'command line input' 'hello world' '5'



