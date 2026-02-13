# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Purpose

Creates an Amazon Lookout for Vision manifest file for the getting started
image segmentation project.
Copies getting started files (manifest/images/masks) to specified S3 bucket.
"""

import logging
import argparse
import sys
import json
import os
from pathlib import Path
import boto3

from boto3.exceptions import S3UploadFailedError

TEMPLATE_MANIFEST_LOCATION = "manifests/template.manifest"
TRAIN_MANIFEST_LOCATION = "manifests/train.manifest"


logger = logging.getLogger(__name__)


def create_train_manifest(local_path, s3_path):
    """
    Creates a Lookout for Vision getting started manifest file
    from a template manifest file.
    param local_path: The local path to the getting started folder.
    param s3_path: The S3 path to where the getting started files are located.
    """
    # Add forward slash at end of s3 path, if missing.
    if not s3_path.endswith("/"):
        s3_path = s3_path + "/"

    logger.info("Creating manifest file from %s.", local_path)
    logger.info("Destination: %s", s3_path)

    template_manifest = Path(local_path) / TEMPLATE_MANIFEST_LOCATION
    getting_started_manifest = Path(local_path) / TRAIN_MANIFEST_LOCATION

    with open(template_manifest, encoding="utf-8")\
        as template_manifest, open(getting_started_manifest, "w", encoding="utf-8")\
            as manifest_file:
        lines = template_manifest.readlines()
        for line in lines:
            json_line = json.loads(line)
            # Update JSON for writing.
            json_line['source-ref'] = s3_path + json_line['source-ref']
            if 'anomaly-mask-ref' in json_line:
                json_line['anomaly-mask-ref'] = s3_path + \
                    json_line['anomaly-mask-ref']

            # Write the updated JSON line.
            output_json = json.dumps(json_line)
            manifest_file.write(output_json + '\n')
            logger.info("Writing json line: %s ", output_json)

        logger.info("Wrote %s JSON Lines.", len(lines))
        logger.info("Finished: Getting started manifest file name: %s",
                    getting_started_manifest)


def copy_local_folder_to_s3(local_path, s3_path):
    """
    Copies a local folder to an S3 path.
    param local_path: The path to the local folder.
    param s3_path: The S3 path to copy the local folder to.
    """

    logger.info("Copying local folder %s to %s", local_path, s3_path)
    # Use default AWS credentials (from environment, ~/.aws/credentials, or IAM role)
    session = boto3.Session()
    s3_resource = session.resource('s3')

    # Remove forward slash at end of s3 path, if present.
    if not s3_path.endswith("/"):
        s3_path = s3_path + "/"

    # Get the S3 bucket name for S3 resource.
    bucket_name, s3_folder_path = s3_path.replace("s3://", "").split("/", 1)
    bucket = s3_resource.Bucket(bucket_name)


    copied_file_count = 0
    # Get all files in folder and upload to S3.
    # dirs are not needed.
    local_path = Path(local_path)
    for root, dirs, files in os.walk(local_path):

        for file in files:
            # ignore any local hidden files.
            if file.startswith("."):
                logger.info("Not copying hidden file %s", file)
                continue

            full_local_path = Path(root) / file
            partial_path = full_local_path.as_posix()[len(local_path.as_posix())+1:]
            destination_file = s3_folder_path + partial_path

            bucket.upload_file(full_local_path.as_posix(), destination_file)
            logger.info("Copied %s to %s", file, "s3://" + bucket_name + "/" + destination_file)

            copied_file_count += 1

    logger.info("Finished copying local folder %s to %s", local_path, s3_path)
    logger.info("%s files copied.", copied_file_count)

def get_manifest_file_location(s3_path):
    """
    Gets the S3 destination for the training manifest file.
    :param s3_path: The S3 destination S3 path.
    """

    # Add forward slash at end of s3 path, if missing.
    if not s3_path.endswith("/"):
        s3_path = s3_path + "/"
    return s3_path + TRAIN_MANIFEST_LOCATION


def main():
    """
    Entry point for getting started script.
    """
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(usage=argparse.SUPPRESS)

    parser.add_argument(
        "s3_path", help="The destination S3 folder for the dataset files.")
    args = parser.parse_args()

    s3_path =args.s3_path

    local_path = Path(os.getcwd()) / "dataset-files"

    print(f"Copying getting started files to {s3_path}")
    try:
        #Create manifest file and copy all files to S3.
        create_train_manifest(local_path, s3_path)
        copy_local_folder_to_s3(local_path, s3_path)

    except FileNotFoundError as file_error:
        print(f"Couldn't open file: {file_error.filename}")
        logger.error("Couldn't open file %s", file_error.filename)
        sys.exit(1)
    except S3UploadFailedError as s3_error:
        print(f"S3 Error: {s3_error}")
        logger.error("S3 Error: %s", s3_error)
        sys.exit(1)

    print(f"Finished copying getting started files to {s3_path}")
    print(f"Create dataset using manifest file: {get_manifest_file_location(s3_path)}")


if __name__ == "__main__":
    main()
