#!/usr/bin/env python3
"""
Inference Results Uploader for DDA Edge Devices

Monitors /aws_dda/inference-results/ directory and uploads inference results
(images and metadata) to S3 periodically.
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
        
        if not self.s3_bucket:
            raise ValueError("s3Bucket configuration is required")
        
        self.s3_client = boto3.client('s3', region_name=self.aws_region)
        
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
    
    def load_uploaded_files(self) -> Dict:
        if os.path.exists(self.uploaded_files_log):
            try:
                with open(self.uploaded_files_log, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load uploaded files log: {e}")
        return {}
    
    def save_uploaded_files(self):
        try:
            with open(self.uploaded_files_log, 'w') as f:
                json.dump(self.uploaded_files, f, indent=2)
        except Exception as e:
            logger.error(f"Could not save uploaded files log: {e}")
    
    def find_files_to_upload(self) -> List[Tuple[str, str]]:
        files_to_upload = []
        
        if not os.path.exists(self.inference_path):
            logger.warning(f"Inference results path does not exist: {self.inference_path}")
            return files_to_upload
        
        patterns = []
        if self.upload_images:
            patterns.extend(['**/*.jpg', '**/*.jpeg', '**/*.png'])
        if self.upload_metadata:
            patterns.extend(['**/*.jsonl', '**/*.json'])
        
        for pattern in patterns:
            for file_path in glob.glob(
                os.path.join(self.inference_path, pattern),
                recursive=True
            ):
                if file_path in self.uploaded_files:
                    continue
                
                rel_path = os.path.relpath(file_path, self.inference_path)
                path_parts = rel_path.split(os.sep)
                
                if len(path_parts) >= 2:
                    model_id = path_parts[0]
                    filename = path_parts[-1]
                    
                    mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
                    date_prefix = mtime.strftime('%Y/%m/%d')
                    
                    s3_key_parts = [self.s3_prefix, model_id, date_prefix, filename]
                    s3_key = '/'.join(filter(None, s3_key_parts))
                    
                    files_to_upload.append((file_path, s3_key))
        
        return files_to_upload[:self.batch_size]
    
    def upload_file(self, local_path: str, s3_key: str) -> bool:
        try:
            content_type = 'application/octet-stream'
            if local_path.endswith(('.jpg', '.jpeg')):
                content_type = 'image/jpeg'
            elif local_path.endswith('.png'):
                content_type = 'image/png'
            elif local_path.endswith(('.json', '.jsonl')):
                content_type = 'application/json'
            
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
        files_to_upload = self.find_files_to_upload()
        
        if not files_to_upload:
            logger.debug("No new files to upload")
            return {'uploaded': 0, 'failed': 0}
        
        logger.info(f"Found {len(files_to_upload)} file(s) to upload")
        
        uploaded_count = 0
        failed_count = 0
        
        for local_path, s3_key in files_to_upload:
            if self.upload_file(local_path, s3_key):
                self.uploaded_files[local_path] = {
                    's3_key': s3_key,
                    'uploaded_at': datetime.utcnow().isoformat(),
                    'size_bytes': os.path.getsize(local_path)
                }
                uploaded_count += 1
            else:
                failed_count += 1
        
        if uploaded_count > 0:
            self.save_uploaded_files()
        
        logger.info(f"Upload batch complete: {uploaded_count} uploaded, {failed_count} failed")
        return {'uploaded': uploaded_count, 'failed': failed_count}
    
    def cleanup_old_files(self):
        if self.retention_days <= 0:
            return
        
        cutoff_time = datetime.now() - timedelta(days=self.retention_days)
        deleted_count = 0
        
        for file_path, info in list(self.uploaded_files.items()):
            if not os.path.exists(file_path):
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
        logger.info("Starting InferenceUploader service")
        
        iteration = 0
        while True:
            try:
                self.upload_batch()
                
                iteration += 1
                if iteration % 10 == 0:
                    self.cleanup_old_files()
                
                time.sleep(self.upload_interval)
                
            except KeyboardInterrupt:
                logger.info("Shutting down InferenceUploader")
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(60)

def validate_config(config: Dict) -> Tuple[bool, str]:
    """Validate configuration parameters"""
    try:
        # Validate upload interval
        upload_interval = config.get('uploadIntervalSeconds', 10)
        if not isinstance(upload_interval, (int, float)) or upload_interval <= 0:
            return False, "uploadIntervalSeconds must be a positive number"
        
        # Validate batch size
        batch_size = config.get('batchSize', 100)
        if not isinstance(batch_size, int) or batch_size <= 0:
            return False, "batchSize must be a positive integer"
        
        # Validate retention days
        retention_days = config.get('localRetentionDays', 7)
        if not isinstance(retention_days, int) or retention_days < 0:
            return False, "localRetentionDays must be a non-negative integer"
        
        # Validate S3 bucket
        s3_bucket = config.get('s3Bucket', '')
        if not s3_bucket:
            return False, "s3Bucket is required"
        
        # Validate AWS region
        aws_region = config.get('awsRegion', 'us-east-1')
        if not isinstance(aws_region, str) or not aws_region:
            return False, "awsRegion must be a non-empty string"
        
        return True, "Configuration is valid"
    except Exception as e:
        return False, f"Configuration validation error: {str(e)}"


def load_configuration() -> Dict:
    """Load configuration from multiple sources with proper precedence"""
    
    # 1. Start with recipe defaults (from recipe.yaml)
    config = {
        'inferenceResultsPath': '/aws_dda/inference-results',
        'uploadIntervalSeconds': 10,  # From recipe.yaml
        'batchSize': 100,
        'localRetentionDays': 7,
        'uploadImages': True,
        'uploadMetadata': True,
        's3Bucket': '',
        's3Prefix': '',
        'awsRegion': 'us-east-1'
    }
    
    # 2. Try to load from Greengrass config file (if it exists)
    config_path = os.environ.get('GG_CONFIG_FILE', '/tmp/config.json')
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                gg_config = json.load(f)
                config.update(gg_config)
                logger.info(f"Loaded configuration from {config_path}")
        except Exception as e:
            logger.warning(f"Could not load Greengrass config from {config_path}: {e}")
    
    # 3. Override with environment variables (highest priority)
    env_overrides = {
        'INFERENCE_RESULTS_PATH': 'inferenceResultsPath',
        'UPLOAD_INTERVAL_SECONDS': 'uploadIntervalSeconds',
        'BATCH_SIZE': 'batchSize',
        'LOCAL_RETENTION_DAYS': 'localRetentionDays',
        'UPLOAD_IMAGES': 'uploadImages',
        'UPLOAD_METADATA': 'uploadMetadata',
        'S3_BUCKET': 's3Bucket',
        'S3_PREFIX': 's3Prefix',
        'AWS_REGION': 'awsRegion'
    }
    
    for env_var, config_key in env_overrides.items():
        if env_var in os.environ:
            value = os.environ[env_var]
            
            try:
                # Type conversion based on expected type
                if config_key in ['uploadIntervalSeconds', 'batchSize', 'localRetentionDays']:
                    config[config_key] = int(value)
                elif config_key in ['uploadImages', 'uploadMetadata']:
                    config[config_key] = value.lower() in ('true', '1', 'yes')
                else:
                    config[config_key] = value
                
                logger.info(f"Overriding {config_key} from environment variable {env_var}={value}")
            except ValueError as e:
                logger.error(f"Invalid value for {env_var}: {value} - {str(e)}")
    
    return config


def main():
    """Entry point"""
    # Load configuration from multiple sources
    config = load_configuration()
    
    # Validate configuration
    is_valid, validation_msg = validate_config(config)
    if not is_valid:
        logger.error(f"Configuration validation failed: {validation_msg}")
        sys.exit(1)
    
    logger.info("Configuration loaded successfully:")
    logger.info(f"  Inference Path: {config['inferenceResultsPath']}")
    logger.info(f"  Upload Interval: {config['uploadIntervalSeconds']}s")
    logger.info(f"  Batch Size: {config['batchSize']}")
    logger.info(f"  Retention: {config['localRetentionDays']} days")
    logger.info(f"  S3 Bucket: {config['s3Bucket']}")
    logger.info(f"  S3 Prefix: {config['s3Prefix']}")
    logger.info(f"  AWS Region: {config['awsRegion']}")
    logger.info(f"  Upload Images: {config['uploadImages']}")
    logger.info(f"  Upload Metadata: {config['uploadMetadata']}")
    
    # Create and run uploader
    uploader = InferenceUploader(config)
    uploader.run()

if __name__ == '__main__':
    main()
