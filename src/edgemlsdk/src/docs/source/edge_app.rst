Overview
========

The Edge Application is a self-contained program specifically designed for efficient image and video processing. 
It empowers developers to define and customize analysis on a per-camera basis, allowing for a more targeted and versatile approach to data handling. 
Within the framework of the application, users can configure settings and specifications that are tailored to individual camera feeds.

To accomplish this, the Edge Application constructs the necessary GStreamer pipelines, a powerful tool for handling media processing. 
These pipelines are series of processing elements connected in a particular order to perform the required analyses. 
They can manage tasks such as filtering, conversion, and combination of media data, providing a streamlined approach to complex multimedia handling.

The implementation of per-camera analysis through GStreamer pipelines offers the flexibility to apply specific algorithms or treatments to different camera inputs, thereby maximizing efficiency and precision in processing. 
Whether it's facial recognition, motion detection, or any other specialized analysis, the Edge Application's adaptable architecture provides a robust platform to cater to diverse and complex processing needs.

Deploying as a Panorama Application
===================================

Deploying as a Greengrass Component
===================================

Input Parameters
================

- **trace_level**: Specifies the trace level of the edge application. Valid inputs are "Error", "Warning", "Information", "Verbose", or "Debug". This parameter will also set the GST_DEBUG level unless specified in the environment dictionary. The mapping is as follows:
  - Error = `GST_LEVEL_ERROR`
  - Warning = `GST_LEVEL_WARNING`
  - Information = `GST_LEVEL_FIXME`
  - Verbose = `GST_LEVEL_INFO`
  - Debug = `GST_LEVEL_DEBUG`

- **environment**: JSON dictionary that sets environment variables.
