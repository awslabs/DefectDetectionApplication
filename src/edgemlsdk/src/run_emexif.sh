#
#
# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
#

export GST_PLUGIN_PATH=/code/build/Release/lib:/code/build/Debug/lib
 

set -e
# Download example images showcasing each possible EXIF orientation. 
if [ -z "$(ls -A exif-orientation-examples)" ]; then
    # Copyright (c) 2010 Dave Perrett, http://recursive-design.com/
    git clone https://github.com/recurser/exif-orientation-examples.git
fi
 
rm -rf output_images
mkdir -p output_images
 
for i in {0..8}
do
    gst-launch-1.0 filesrc blocksize=-1 location=exif-orientation-examples/Landscape_${i}.jpg ! emexifextract ! jpegdec idct-method=2 ! videoflip method=automatic ! jpegenc idct-method=2 quality=100 ! filesink location=output_images/Landscape_${i}_output.jpg
done
 
for i in {0..8}
do
    gst-launch-1.0 filesrc blocksize=-1 location=exif-orientation-examples/Portrait_${i}.jpg ! emexifextract ! jpegdec idct-method=2 ! videoflip method=automatic ! jpegenc idct-method=2 quality=100 ! filesink location=output_images/Portrait_${i}_output.jpg
done

rm -rf output_images/
