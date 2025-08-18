.. |br| raw:: html

    <br />

.. _gst:

=========
GStreamer
=========

Overview
--------

EdgeML-SDK provides built in objects to interact with GStreamer in a more object oriented design.  You are, of course, welcome to continue to use GStreamer through it's APIs.  The goal of our objects is to remove a lot of the tedium around creating, maintaining, and interacting with GStreamer objects and present the main functionality in a manner that is easier to use.  The two objects we provide to achieve this goal are:

-   :ref:`Pipeline <pipeline>`

    Object that wraps a GStreamer pipeline

-   :ref:`Pipeline Manager <pipeline_manager>`

    Object that maintains the lifecycle of several Pipeline objects


.. _pipeline:

Pipeline
--------

This object is used to create and manage a specific GStreamer pipeline.  To create a pipeline object you will need the following information:

-   **Pipeline Id**: 

    User defined string to uniquely identify the pipeline.  While you can create multiple pipelines with the same ID this will cause errors when trying to add them to the Pipeline Manager

-   **Pipeline Definition**:  

    The string that defines how to build the gstreamer pipeline.  This would be the same string you would pass to gst-launch-1.0.  This string is allowed to have reference to variables.  More information on how to use variables in your pipeline definition in :ref:`Variables <variables>`

-   **IApp**:

    Object that acts as a Property Delegate, to provide variable values, and a Credential Provider, to access AWS resources that may be needed by some variables.

Example Pipeline Creation
^^^^^^^^^^^^^^^^^^^^^^^^^

..  tabs::

    ..  group-tab:: C++

        .. note:: link against libpanorama.gst.so and libpanorama.app.so

        .. literalinclude:: ../../../samples/C++/Gst/simple_pipeline.cpp
            :language: cpp
            :linenos:

    ..  group-tab:: Python

        .. literalinclude:: ../../../samples/Python/Gst/simple_pipeline.py
            :language: python
            :linenos:

.. _variables:

Pipeline Variables
^^^^^^^^^^^^^^^^^^

When definining a pipeline you can assign the value of a plugin property to a variable.  Variables need to be specified in a Property Delegate, see :ref:`Properties <properties>` for more details on Property Delegates, and the must take the following form:

.. code-block:: json

    {
        "variable-name": 
        {
            "type": "string/secretsmanager/s3",
            "immutable": true/false,
            "value": "property value"
        }
    }

-   **type**

    The type of variable.  Currently supported values are 'string', 'secretsmanager', and 's3'

-   **immutable**

    A boolean that indicates if this value changes then the pipeline must restart.  Value of true will cause the pipeline to restart if this variable changes.  Default is false.

-   **value**

    The value to of the variable.  Depending on the type of variable this value will be expanded in different ways.

Variables are referenced in the pipeline by encapsulating the name of the variable in '${}'.

.. note:: Any plugin that uses a variable MUST set the name property

**String Variable**

This type of variable simply returns the value contained within the 'value' field when this variable is evaluated.

**Secrets Manager Variable**

This type of variable will retrieve a value from a Secrets Manager object in AWS.  The secret must be in the form of key/value pairs.  The value of this variable should be in the form "<arn>,<key>" where the <arn> is the arn of the Secrets Manager object and <key> specifies the key in that Secrets Manager object you wish to retrieve.  This object will check if the value in the Secrets Manager object has changed each time the variable is evaluated.  This is useful if you want to provide credentials to a pipeline without including them in plain text anywhere.

**S3 Variable**

This type of variable will download an object from S3 to the local file system.  The value of this variable should be in the form "<arn>,localPath=<path>" where the <arn> is the arn of the S3 object you wish to download and <path> is the relative or absolute path to save the object in your local file system.  Evaulation of this syncrhonous and will delay the creation of a pipeline until the object is fully downloaded.  This is useful if you need to download a large file, like a model, for the pipeline.

..  tabs::

    ..  group-tab:: C++

        .. note:: link against libpanorama.gst.so and libpanorama.app.so

        .. literalinclude:: ../../../samples/C++/Gst/variables_pipeline.cpp
            :language: cpp
            :linenos:

    ..  group-tab:: Python

        .. literalinclude:: ../../../samples/Python/Gst/variables_pipeline.py
            :language: python
            :linenos:

.. _pipeline_manager:

Pipeline Manager
----------------

The pipeline manager is an object that manages the lifetime of several pipeline objects.  Pipeline objects can be created and added/removed to/fom the manager manually.  However, the manager will interface to your Property Delegates and look for the "pipelines" property.  If that property is defined in any of your property delegates then it will create the pipelines specified in that object.  Variables can be used in those pipeline defintions as detailed above.

The pipelines property should take the following form

.. code-block:: json

    {
        "pipelines": 
        [
            {
                "id": "pipeline-id1",
                "definition": "your-pipeline-definition"
            },
            {
                "id": "pipeline-id2",
                "definition": "another-pipeline-definition"
            }
        ]
    }

As pipeline definitions are added/removed/modified in the pipelines property the Pipeline Manager will update/restart the pipelines as appropriate.  

Example
^^^^^^^

..  tabs::

    ..  group-tab:: C++

        .. note:: link against libpanorama.gst.so and libpanorama.app.so

        .. literalinclude:: ../../../samples/C++/Gst/pipeline_manager.cpp
            :language: cpp
            :linenos:

    ..  group-tab:: Python

        .. literalinclude:: ../../../samples/Python/Gst/pipeline_manager.py
            :language: python
            :linenos:

.. TODO: Sphinx/Doxygen really being obnoxious with this.  Circle back on this.
.. API
.. ---

.. ..  tabs::

..     ..  group-tab:: C++

..         **Interfaces**

..         .. doxygenstruct:: Panorama::IPipeline
..             :members:

..         .. doxygenstruct:: Panorama::IPipelineManager
..             :members:

..         **Factory Methods**

..         .. doxygenfunction:: Panorama::GStreamer::Initialize(int argc, char* argv[], int gstLogLevel = GST_LEVEL_FIXME)
..         .. doxygenfunction:: Panorama::GStreamer::Initialize(int gstLogLevel = GST_LEVEL_FIXME)
..         .. doxygenfunction:: Panorama::GStreamer::Shutdown()
..         .. doxygenfunction:: Panorama::GStreamer::MakeCom(GstElement* element)
..         .. doxygenfunction:: Panorama::GStreamer::AddPayloadToBuffer(IPayload* payload, GstBuffer* buf, const char* id)
..         .. doxygenfunction:: Panorama::GStreamer::GetPayloadFromBuffer(IPayload** ppObj, GstBuffer* buf, const char* id)

..     ..  group-tab:: Python

..         **Classes**

..         .. autoclass:: panrama.gst.pipeline
..             :members:
..             :undoc-members:

..         **Factory Methods**

..         .. automethod:: panorama.gst.initialize
..         .. automethod:: panorama.gst.shutdown
..         .. automethod:: panorama.gst.pipeline.create