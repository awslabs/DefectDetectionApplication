#!/usr/bin/env python3
"""
Inference Results Uploader for DDA Edge Devices

Monitors /aws_dda/inference-results/ directory and uploads inference results
(images and metadata) to S3 periodically.

Features:
- Batch uploads every N seconds
- Maintains directory structure in S3
- Cleans up old local files based on retention policy
- Handles network interruptions with retry logic
- Logs upload status
"""

import os
import sys
import json
import time
import logging
import boto3
import glob
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Tuple

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('InferenceUploader')

class InferenceUploader:
    def __init__(self, config: Dict):
        self.config = config
        self.inference_path = config.get('inferenceResultsPath', '/aws_dda/inference-results')
        self.upload_interval = config.get('uploadIntervalSeconds', 300)
        self.batch_size = config.get('batchSize', 100)
        self.retention_days = config.get('localRetentionDays', 7)
        self.upload_images = config.get('uploadImages', True)
        self.upload_metadata = config.get('uploadMetadata', True)
        self.s3_bucket = config.get('s3Bucket', '')
        self.s3_prefix = config.get('s3Prefix', '')
        self.aws_region = config.get('awsRegion', 'us-east-1')
        
        # Validate configuration
        if not self.s3_bucket:
            raise ValueError("s3Bucket configuration is required")
        
        # Initialize S3 client
        self.s3_client = boto3.client('s3', region_name=self.aws_region)
        
        # Track uploaded files
        self.uploaded_files_log = os.path.join(
            os.path.dirname(self.inference_path),
            '.inference_uploader_state.json'
        )
        self.uploaded_files = self.load_uploaded_files()
        
        logger.info(f"InferenceUploader initialized")
        logger.info(f"  Monitoring: {self.inference_path}")
        logger.info(f"  S3 Bucket: {self.s3_bucket}")
        logger.info(f"  S3 Prefix: {self.s3_prefix}")
        logger.info(f"  Upload Interval: {self.upload_interval}s")
        logger.info(f"  Batch Size: {self.batch_size}")
        logger.info(f"  Retention: {self.retention_days} days")
    
    def load_uploaded_files(self) -> Dict:
        """Load the list of already uploaded files"""
        if os.path.exists(self.uploaded_files_log):
            try:
                with open(self.uploaded_files_log, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load uploaded files log: {e}")
        return {}
    
    def save_uploaded_files(self):
        """Save the list of uploaded files"""
        try:
            with open(self.uploaded_files_log, 'w') as f:
                json.dump(self.uploaded_files, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save uploaded files log: {e}")
    
    def find_files_to_upload(self) -> List[Tuple[str, str]]:
        """
        Find inference result files that need to be uploaded.
        Returns list of (local_path, s3_key) tuples.
        """
        files_to_upload = []
        
        # Find all model directories
        if not os.path.exists(self.inference_path):
            logger.warning(f"Inference results path does not exist: {self.inference_path}")
            return files_to_upload
        
        # Scan for files
        patterns = []
        if self.upload_images:
            patterns.append('**/*.jpg')
            patterns.append('**/*.jpeg')
            patterns.append('**/*.png')
        if self.upload_metadata:
            patterns.append('**/*.jsonl')
            patterns.append('**/*.json')
        
        for pattern in patterns:
            for file_path in glob.glob(
                os.path.join(self.inference_path, pattern),
                recursive=True
            ):
                # Skip if already uploaded
                if file_path in self.uploaded_files:
                    continue
                
                # Generate S3 key maintaining directory structure
                rel_path = os.path.relpath(file_path, self.inference_path)
                
                # Add timestamp-based prefix for organization
                # Extract model_id from path (first directory level)
                path_parts = rel_path.split(os.sep)
                if len(path_parts) >= 2:
                    model_id = path_parts[0]
                    filename = path_parts[-1]
                    
                    # Get file modification time for date-based organization
                    mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
                    date_prefix = mtime.strftime('%Y/%m/%d')
                    
                    # Build S3 key: prefix/model_id/YYYY/MM/DD/filename
                    s3_key_parts = [self.s3_prefix, model_id, date_prefix, filename]
                    s3_key = '/'.join(filter(None, s3_key_parts))
                    
                    files_to_upload.append((file_path, s3_key))
        
        return files_to_upload[:self.batch_size]
    
    def upload_file(self, local_path: str, s3_key: str) -> bool:
        """Upload a single file to S3"""
        try:
            # Determine content type
            content_type = 'application/octet-stream'
            if local_path.endswith('.jpg') or local_path.endswith('.jpeg'):
                content_type = 'image/jpeg'
            elif local_path.endswith('.png'):
                content_type = 'image/png'
            elif local_path.endswith('.json') or local_path.endswith('.jsonl'):
                content_type = 'application/json'
            
            # Upload to S3
            self.s3_client.upload_file(
                local_path,
                self.s3_bucket,
                s3_key,
                ExtraArgs={
                    'ContentType': content_type,
                    'Metadata': {
                        'uploaded-by': 'inference-uploader',
                        'upload-timestamp': datetime.utcnow().isoformat()
                    }
                }
            )
            
            logger.info(f"Uploaded: {local_path} -> s3://{self.s3_bucket}/{s3_key}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to upload {local_path}: {e}")
            return False
    
    def upload_batch(self) -> Dict:
        """Upload a batch of files"""
        files_to_upload = self.find_files_to_upload()
        
        if not files_to_upload:
            logger.debug("No new files to upload")
            return {'uploaded': 0, 'failed': 0}
        
        logger.info(f"Found {len(files_to_upload)} file(s) to upload")
        
        uploaded_count = 0
        failed_count = 0
        
        for local_path, s3_key in files_to_upload:
            if self.upload_file(local_path, s3_key):
                # Mark as uploaded
                self.uploaded_files[local_path] = {
                    's3_key': s3_key,
                    'uploaded_at': datetime.utcnow().isoformat(),
                    'size_bytes': os.path.getsize(local_path)
                }
                uploaded_count += 1
            else:
                failed_count += 1
        
        # Save state
        if uploaded_count > 0:
            self.save_uploaded_files()
        
        logger.info(f"Upload batch complete: {uploaded_count} uploaded, {failed_count} failed")
        return {'uploaded': uploaded_count, 'failed': failed_count}
    
    def cleanup_old_files(self):
        """Remove local files older than retention period"""
        if self.retention_days <= 0:
            return
        
        cutoff_time = datetime.now() - timedelta(days=self.retention_days)
        deleted_count = 0
        
        for file_path, info in list(self.uploaded_files.items()):
            # Check if file was uploaded and is old enough
            if not os.path.exists(file_path):
                # File already deleted, remove from tracking
                del self.uploaded_files[file_path]
                continue
            
            uploaded_at = datetime.fromisoformat(info['uploaded_at'])
            if uploaded_at < cutoff_time:
                try:
                    os.remove(file_path)
                    logger.info(f"Deleted old file: {file_path}")
                    deleted_count += 1
                    del self.uploaded_files[file_path]
                except Exception as e:
                    logger.error(f"Failed to delete {file_path}: {e}")
        
        if deleted_count > 0:
            self.save_uploaded_files()
            logger.info(f"Cleanup complete: {deleted_count} file(s) deleted")
    
    def run(self):
        """Main loop"""
        logger.info("Starting InferenceUploader service")
        
        iteration = 0
        while True:
            try:
                # Upload batch
                result = self.upload_batch()
                
                # Cleanup old files every 10 iterations (~50 minutes if interval is 5 min)
                iteration += 1
                if iteration % 10 == 0:
                    self.cleanup_old_files()
                
                # Wait for next interval
                time.sleep(self.upload_interval)
                
            except KeyboardInterrupt:
                logger.info("Shutting down InferenceUploader")
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(60)  # Wait a minute before retrying

def main():
    """Entry point"""
    # Greengrass provides configuration via IPC
    # For simplicity, we'll use environment variables and a config file fallback
    
    # Default configuration
    config = {
        'inferenceResultsPath': os.environ.get('INFERENCE_RESULTS_PATH', '/aws_dda/inference-results'),
        'uploadIntervalSeconds': int(os.environ.get('UPLOAD_INTERVAL_SECONDS', '300')),
        'batchSize': int(os.environ.get('BATCH_SIZE', '100')),
        'localRetentionDays': int(os.environ.get('LOCAL_RETENTION_DAYS', '7')),
        'uploadImages': os.environ.get('UPLOAD_IMAGES', 'true').lower() == 'true',
        'uploadMetadata': os.environ.get('UPLOAD_METADATA', 'true').lower() == 'true',
        's3Bucket': os.environ.get('S3_BUCKET', ''),
        's3Prefix': os.environ.get('S3_PREFIX', ''),
        'awsRegion': os.environ.get('AWS_REGION', 'us-east-1')
    }
    
    # Try to load from Greengrass configuration file
    # Greengrass writes component configuration to a JSON file
    config_file = os.environ.get('AWS_GG_NUCLEUS_DOMAIN_SOCKET_FILEPATH_FOR_COMPONENT')
    if config_file:
        # In a real Greengrass environment, we'd use IPC to get configuration
        # For now, we'll use environment variables
        logger.info("Running in Greengrass environment")
    
    # Validate required configuration
    if not config['s3Bucket']:
        logger.error("S3_BUCKET environment variable is required")
        sys.exit(1)
    
    logger.info(f"Starting InferenceUploader with configuration:")
    logger.info(f"  S3 Bucket: {config['s3Bucket']}")
    logger.info(f"  S3 Prefix: {config['s3Prefix']}")
    logger.info(f"  Upload Interval: {config['uploadIntervalSeconds']}s")
    logger.info(f"  Inference Path: {config['inferenceResultsPath']}")
    
    # Create and run uploader
    uploader = InferenceUploader(config)
    uploader.run()

if __name__ == '__main__':
    main()
