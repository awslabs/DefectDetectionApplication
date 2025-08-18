.. _gst-plugins:

=================
GStreamer Plugins
=================

The SDK comes shipped with several plugins and tracers.  Below is a list of the available plugins and tracers in this SDK.  Plugins and tracers get installed to /usr/lib/panoramagst so be sure to include that directory when setting your GST_PLUGIN_PATH.

.. toctree::
   :maxdepth: 1

   gate
   emlcapture
   emlfilesrc
   eminfer_marshal

Several of the above plugins make use of the Metadata APIs in EdgeML-SDK.  If you are looking to implement your own GStreamer plugin that consumes/creates GStreamer Metadata that is compatible with this SDK you should familiarize yourself with the Metadata APIs.

.. toctree::
   :maxdepth: 1

   metadata