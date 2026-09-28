#!/usr/bin/env python3
"""
Test script to verify BigQuery authentication setup.
Run this script to test your BigQuery authentication configuration.
"""

import sys
import os
import logging
from dotenv import load_dotenv
from pathlib import Path

# Load environment variables from .env file
env_path = Path(__file__).resolve().parent / "fashion_bot" / ".env"
load_dotenv(dotenv_path=env_path)

# Add the fashion_bot directory to the Python path
sys.path.insert(0, str(Path(__file__).parent))

from fashion_bot.history.bigquery_logger import BigQueryLogger

# Set up logging to see authentication details
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_bigquery_authentication():
    """Test BigQuery authentication with current configuration."""
    
    logger.info("Testing BigQuery Authentication...")
    
    # Show current environment configuration
    project_id = os.getenv('BIGQUERY_PROJECT_ID')
    service_account_key = os.getenv('BIGQUERY_SERVICE_ACCOUNT_KEY')
    service_account_key_base64 = os.getenv('BIGQUERY_SERVICE_ACCOUNT_KEY_BASE64')
    service_account_key_file = os.getenv('BIGQUERY_SERVICE_ACCOUNT_KEY_FILE')
    
    logger.info("Current configuration:")
    logger.info(f"BIGQUERY_PROJECT_ID: {project_id or 'Not set'}")
    logger.info(f"BIGQUERY_SERVICE_ACCOUNT_KEY: {'Set' if service_account_key else 'Not set'}")
    logger.info(f"BIGQUERY_SERVICE_ACCOUNT_KEY_BASE64: {'Set' if service_account_key_base64 else 'Not set'}")
    logger.info(f"BIGQUERY_SERVICE_ACCOUNT_KEY_FILE: {service_account_key_file or 'Not set'}")
    
    try:
        # Initialize BigQuery logger
        bq_logger = BigQueryLogger()
        
        # Test client creation
        logger.info("Attempting to create BigQuery client...")
        client = bq_logger._get_client()
        
        # Test basic BigQuery operation
        logger.info("Testing BigQuery connection...")
        datasets = list(client.list_datasets(max_results=1))
        
        # Try to list datasets (this requires basic BigQuery access)
        projects = [bq_logger.project_id]
        for project in projects:
            datasets = list(client.list_datasets(project=project, max_results=1))
            logger.info(f"Successfully connected to BigQuery project: {project}")
            break
        
        logger.info("✅ BigQuery authentication test PASSED!")
        logger.info("BigQuery logging is properly configured and ready to use.")
        return True
        
    except Exception as e:
        logger.error(f"❌ BigQuery authentication test FAILED: {e}")
        logger.error("Troubleshooting steps:")
        logger.error("1. Check your service account key has BigQuery permissions")
        logger.error("2. Verify your project ID is correct")
        logger.error("3. Ensure your service account key is valid JSON (if using raw key)")
        logger.error("4. Ensure your service account key is properly base64 encoded (if using base64 key)")
        return False

def show_usage():
    """Show usage instructions for different authentication methods."""
    
    print("\n" + "=" * 60)
    print("BigQuery Authentication Setup Options")
    print("=" * 60)
    
    print("\n1. Base64 Encoded Service Account Key (Recommended for production):")
    print("   export BIGQUERY_SERVICE_ACCOUNT_KEY_BASE64='<base64-encoded-json-key>'")
    
    print("\n2. Explicit Service Account Key (JSON String):")
    print("   export BIGQUERY_SERVICE_ACCOUNT_KEY='{\"type\": \"service_account\", ...}'")
    
    print("\n3. Explicit Service Account Key (File Path):")
    print("   export BIGQUERY_SERVICE_ACCOUNT_KEY_FILE=/path/to/key.json")
    
    print("\n4. Application Default Credentials:")
    print("   gcloud auth application-default login")
    print("   # OR")
    print("   export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json")
    
    print("\n5. Project Configuration:")
    print("   export BIGQUERY_PROJECT_ID=your-gcp-project-id")
    
    print("\nAuthentication Priority (highest to lowest):")
    print("1. BIGQUERY_SERVICE_ACCOUNT_KEY_BASE64")
    print("2. BIGQUERY_SERVICE_ACCOUNT_KEY") 
    print("3. BIGQUERY_SERVICE_ACCOUNT_KEY_FILE")
    print("4. Application Default Credentials")
    
    print("\nThen run: python test_bigquery_auth.py")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--help":
        show_usage()
    else:
        success = test_bigquery_authentication()
        
        if not success:
            print("\n" + "=" * 60)
            print("For setup instructions, run: python test_bigquery_auth.py --help")
            print("=" * 60)
            sys.exit(1)
        else:
            print("\n🎉 Your BigQuery setup is ready to use!") 