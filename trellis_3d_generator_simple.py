#!/usr/bin/env python3
"""
Simplified Trellis 3D Model Generator for Zuo Furniture
======================================================

This script generates 3D models for Zuo furniture using the Trellis RunPod API.
It processes items that need 3D assets and uploads them to Supabase storage.

Setup:
1. Set environment variables:
   - SUPABASE_URL=your_supabase_url
   - SUPABASE_SERVICE_KEY=your_service_key
   - TRELLIS_API_URL=your_trellis_api_url
   
2. Install dependencies:
   pip install -r requirements_trellis.txt

Usage:
   python trellis_3d_generator_simple.py --test  # Test with 1 item
   python trellis_3d_generator_simple.py --limit 10  # Process 10 items
   python trellis_3d_generator_simple.py  # Process all pending items
"""

import os
import sys
import time
import logging
import tempfile
import requests
from typing import List, Dict, Optional, Tuple

# Third-party imports with compatibility handling
try:
    from gradio_client import Client, handle_file
except ImportError:
    try:
        from gradio_client import Client
        try:
            from gradio_client import file
            handle_file = file
        except ImportError:
            # For older versions, just use the path directly
            handle_file = lambda x: x
    except ImportError as e:
        print(f"Missing required dependencies: {e}")
        print("Install with: pip install -r requirements_trellis.txt")
        sys.exit(1)

try:
    from supabase import create_client
    from dotenv import load_dotenv
except ImportError as e:
    print(f"Missing required dependencies: {e}")
    print("Install with: pip install -r requirements_trellis.txt")
    sys.exit(1)

# Load environment variables
load_dotenv()

class TrellisGenerator:
    def __init__(self):
        self.setup_logging()
        self.setup_configuration()
        self.setup_clients()
        
    def setup_logging(self):
        """Configure logging"""
        log_format = '%(asctime)s - %(levelname)s - %(message)s'
        logging.basicConfig(
            level=logging.INFO,
            format=log_format,
            handlers=[
                logging.StreamHandler(sys.stdout)
            ]
        )
        self.logger = logging.getLogger(__name__)
        
    def setup_configuration(self):
        """Setup configuration"""
        self.config = {
            # Trellis API
            'TRELLIS_API_URL': os.getenv('TRELLIS_API_URL'),
            
            # 3D Generation Parameters (from your screenshot)
            'SEED': 0,
            'RANDOMIZE_SEED': True,
            'SS_GUIDANCE_STRENGTH': 18.0,
            'SS_SAMPLING_STEPS': 35,  # Integer, not float
            'SLAT_GUIDANCE_STRENGTH': 9.0,
            'SLAT_SAMPLING_STEPS': 35,  # Integer, not float
            'MESH_SIMPLIFY': 0.92,
            'TEXTURE_SIZE': 2048,  # Integer, not float
            
            # Supabase
            'SUPABASE_URL': os.getenv('SUPABASE_URL'),
            'SUPABASE_SERVICE_KEY': os.getenv('SUPABASE_SERVICE_KEY'),
            'SUPABASE_BUCKET': 'zuo-generated',
            
            # Processing
            'BATCH_SIZE': 5,
            'MAX_RETRIES': 3,
            'RETRY_DELAY': 30
        }
        
        # Check all required environment variables
        if not self.config['SUPABASE_URL'] or not self.config['SUPABASE_SERVICE_KEY']:
            raise ValueError("Missing Supabase configuration. Set SUPABASE_URL and SUPABASE_SERVICE_KEY environment variables.")
        
        if not self.config['TRELLIS_API_URL']:
            raise ValueError("Missing TRELLIS_API_URL environment variable.")
        
    def setup_clients(self):
       """Initialise Supabase + Trellis clients (Basic‑Auth via URL)."""
       try:
           # Supabase
           self.supabase = create_client(
               self.config['SUPABASE_URL'],
               self.config['SUPABASE_SERVICE_KEY']
           )
   
           # Build URL with Basic‑Auth creds
           base_url = os.getenv("TRELLIS_API_HOST")  # host only, e.g. ygr2qv52nkrj75-7860.proxy.runpod.net
           user     = os.getenv("RUNPOD_USERNAME")
           pwd      = os.getenv("RUNPOD_PASSWORD")
   
           if not (base_url and user and pwd):
               raise ValueError("Missing TRELLIS_API_HOST or RunPod credentials")
   
           api_url = f"https://{user}:{pwd}@{base_url}/"
   
           self.trellis_client = Client(api_url)      # no auth kwarg
           self.logger.info("Clients initialized successfully")
   
       except Exception as e:
           self.logger.error(f"Failed to set up clients: {e}")
           raise

               
    def get_next_pending_item(self) -> Optional[Dict]:
        """Get the next single item that needs 3D model generation"""
        try:
            # Get one item from zuo_3d_embedding where has_asset = False
            query = self.supabase.table('zuo_3d_embedding').select('*').eq('has_asset', False).limit(1)
            embedding_result = query.execute()
            
            if not embedding_result.data:
                return None
                
            item = embedding_result.data[0]
            zuo_item_no = item['Zuo_Item_No']
            
            self.logger.info(f"Checking item: {zuo_item_no}")
            
            # Get catalog info
            catalog_result = self.supabase.table('zuo_catalog').select('Main_Category', 'Item_Status').eq('Zuo_Item_No', zuo_item_no).execute()
            if not catalog_result.data:
                self.logger.debug(f"No catalog data for {zuo_item_no}, marking as processed")
                # Mark as processed to skip in future
                self.supabase.table('zuo_3d_embedding').update({'has_asset': True}).eq('Zuo_Item_No', zuo_item_no).execute()
                return self.get_next_pending_item()  # Recursively get next
                
            catalog_data = catalog_result.data[0]
            
            # Skip outdoor and discontinued items
            if catalog_data.get('Main_Category') == 'Outdoor':
                self.logger.debug(f"Skipping outdoor item {zuo_item_no}")
                self.supabase.table('zuo_3d_embedding').update({'has_asset': True}).eq('Zuo_Item_No', zuo_item_no).execute()
                return self.get_next_pending_item()
                
            if catalog_data.get('Item_Status') == 'DISC':
                self.logger.debug(f"Skipping discontinued item {zuo_item_no}")
                self.supabase.table('zuo_3d_embedding').update({'has_asset': True}).eq('Zuo_Item_No', zuo_item_no).execute()
                return self.get_next_pending_item()
            
            # Get image info
            image_result = self.supabase.table('zuo_images').select('Single_Image_1_URL', 'Single_Image_2_URL', 'Single_Image_3_URL').eq('Zuo_Item_No', zuo_item_no).execute()
            if not image_result.data or not image_result.data[0].get('Single_Image_1_URL'):
                self.logger.debug(f"No valid image for {zuo_item_no}")
                self.supabase.table('zuo_3d_embedding').update({'has_asset': True}).eq('Zuo_Item_No', zuo_item_no).execute()
                return self.get_next_pending_item()
            
            # Combine data
            combined_item = {
                'zuo_item_no': zuo_item_no,
                'main_category': catalog_data.get('Main_Category'),
                'item_status': catalog_data.get('Item_Status'),
                'single_image_1_url': image_result.data[0].get('Single_Image_1_URL'),
                'single_image_2_url': image_result.data[0].get('Single_Image_2_URL'),
                'single_image_3_url': image_result.data[0].get('Single_Image_3_URL'),
                'has_asset': item.get('has_asset', False)
            }
            
            self.logger.info(f"✓ Valid item found: {zuo_item_no} ({catalog_data.get('Main_Category')})")
            return combined_item
            
        except Exception as e:
            self.logger.error(f"Failed to get next pending item: {e}")
            return None
            
    def download_image(self, url: str) -> str:
        """Download image to temporary file"""
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            
            with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as temp_file:
                temp_file.write(response.content)
                return temp_file.name
                
        except Exception as e:
            self.logger.error(f"Failed to download image {url}: {e}")
            raise
            
    def generate_3d_model(self, item: Dict) -> str:
       """Generate 3D model using Trellis API"""
       try:
           # Download primary image
           image_path = self.download_image(item['single_image_1_url'])
           
           self.logger.info(f"Generating 3D model for {item['zuo_item_no']}")
           
           # For gradio_client 0.5.3, use positional arguments
           result = self.trellis_client.predict(
               handle_file(image_path),  # image
               self.config['SEED'],
               self.config['RANDOMIZE_SEED'],
               self.config['SS_GUIDANCE_STRENGTH'],
               int(self.config['SS_SAMPLING_STEPS']),
               self.config['SLAT_GUIDANCE_STRENGTH'],
               int(self.config['SLAT_SAMPLING_STEPS']),
               self.config['MESH_SIMPLIFY'],
               int(self.config['TEXTURE_SIZE']),
               api_name="/generate_wrapper"
           )
           
           # Clean up temp image
           os.unlink(image_path)
           
           # Result is a tuple: (video_dict, glb_filepath)
           video_result, glb_filepath = result
           
           self.logger.info(f"3D model generated successfully for {item['zuo_item_no']}")
           return glb_filepath
           
       except Exception as e:
           self.logger.error(f"Failed to generate 3D model for {item['zuo_item_no']}: {e}")
           raise
            
    def upload_to_supabase(self, item: Dict, glb_filepath: str) -> str:
        """Upload GLB file to Supabase storage"""
        try:
            # Read GLB file
            with open(glb_filepath, 'rb') as file:
                glb_data = file.read()
                
            # Upload to Supabase storage
            filename = f"{item['zuo_item_no']}.glb"
            
            self.logger.info(f"Uploading {filename} to Supabase storage...")
            
            # Try to upload (will overwrite if exists)
            try:
                # First try to delete existing file if it exists
                try:
                    self.supabase.storage.from_(self.config['SUPABASE_BUCKET']).remove([filename])
                except:
                    pass  # File doesn't exist, that's fine
                
                # Upload the new file
                response = self.supabase.storage.from_(self.config['SUPABASE_BUCKET']).upload(
                    path=filename,
                    file=glb_data,
                    file_options={"content-type": "model/gltf-binary"}
                )
            except Exception as upload_error:
                # If bucket doesn't exist, create it
                if "Bucket not found" in str(upload_error):
                    self.logger.info(f"Creating bucket {self.config['SUPABASE_BUCKET']}")
                    self.supabase.storage.create_bucket(self.config['SUPABASE_BUCKET'], {"public": True})
                    response = self.supabase.storage.from_(self.config['SUPABASE_BUCKET']).upload(
                        path=filename,
                        file=glb_data,
                        file_options={"content-type": "model/gltf-binary"}
                    )
                else:
                    raise upload_error
            
            # Get public URL
            public_url = self.supabase.storage.from_(self.config['SUPABASE_BUCKET']).get_public_url(filename)
            
            self.logger.info(f"Successfully uploaded {filename}")
            return public_url
            
        except Exception as e:
            self.logger.error(f"Failed to upload GLB for {item['zuo_item_no']}: {e}")
            raise
            
    def update_database(self, item: Dict, asset_url: str):
        """Update database with generated asset information"""
        try:
            # Update zuo_3d_embedding table
            update_data = {
                'asset_url': asset_url,
                'asset_url_full': asset_url,
                'has_asset': True
            }
            
            result = self.supabase.table('zuo_3d_embedding').update(update_data).eq('Zuo_Item_No', item['zuo_item_no']).execute()
            
            if result.data:
                self.logger.info(f"✅ Database updated successfully for {item['zuo_item_no']}")
            else:
                raise Exception("No rows updated")
                
        except Exception as e:
            self.logger.error(f"Failed to update database for {item['zuo_item_no']}: {e}")
            raise
            
    def process_item(self, item: Dict) -> bool:
        """Process a single furniture item"""
        glb_filepath = None
        
        try:
            self.logger.info(f"Processing {item['zuo_item_no']} ({item['main_category']})")
            
            # Generate 3D model
            glb_filepath = self.generate_3d_model(item)
            
            # Upload to Supabase
            asset_url = self.upload_to_supabase(item, glb_filepath)
            
            # Update database
            self.update_database(item, asset_url)
            
            self.logger.info(f"✅ Successfully processed {item['zuo_item_no']}")
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Failed to process {item['zuo_item_no']}: {e}")
            return False
            
        finally:
            # Cleanup temp GLB file
            if glb_filepath and os.path.exists(glb_filepath):
                try:
                    os.unlink(glb_filepath)
                except Exception:
                    pass
                    
    def run(self, limit: Optional[int] = None, test_mode: bool = False):
        """Run the 3D generation process"""
        try:
            if test_mode:
                limit = 1
                
            self.logger.info(f"Starting Trellis 3D Model Generation{'(TEST MODE)' if test_mode else ''}")
            
            # Process items one by one
            success_count = 0
            failed_count = 0
            processed_count = 0
            
            while True:
                # Check if we've reached the limit
                if limit and processed_count >= limit:
                    self.logger.info(f"Reached limit of {limit} items")
                    break
                
                # Get next item that needs processing
                item = self.get_next_pending_item()
                if not item:
                    self.logger.info("No more items to process")
                    break
                
                processed_count += 1
                self.logger.info(f"\n{'='*50}")
                self.logger.info(f"Processing item {processed_count}/{limit if limit else '?'}: {item['zuo_item_no']}")
                self.logger.info(f"Category: {item['main_category']}")
                self.logger.info(f"{'='*50}")
                
                # Process this item with retries
                retry_count = 0
                item_success = False
                
                while retry_count < self.config['MAX_RETRIES'] and not item_success:
                    try:
                        if self.process_item(item):
                            success_count += 1
                            item_success = True
                            self.logger.info(f"✅ Successfully processed {item['zuo_item_no']}")
                        else:
                            retry_count += 1
                            if retry_count < self.config['MAX_RETRIES']:
                                self.logger.warning(f"❌ Failed processing {item['zuo_item_no']}, retrying in {self.config['RETRY_DELAY']} seconds... (attempt {retry_count + 1}/{self.config['MAX_RETRIES']})")
                                time.sleep(self.config['RETRY_DELAY'])
                            else:
                                failed_count += 1
                                self.logger.error(f"❌ Failed processing {item['zuo_item_no']} after {self.config['MAX_RETRIES']} attempts")
                                # Mark as processed to avoid infinite loops
                                try:
                                    self.supabase.table('zuo_3d_embedding').update({'has_asset': True}).eq('Zuo_Item_No', item['zuo_item_no']).execute()
                                    self.logger.info(f"Marked {item['zuo_item_no']} as processed to skip in future")
                                except Exception as mark_error:
                                    self.logger.error(f"Failed to mark {item['zuo_item_no']} as processed: {mark_error}")
                                
                    except KeyboardInterrupt:
                        self.logger.info("\n🛑 Process interrupted by user")
                        return
                    except Exception as e:
                        self.logger.error(f"💥 Unexpected error processing {item['zuo_item_no']}: {e}")
                        retry_count += 1
                        if retry_count >= self.config['MAX_RETRIES']:
                            failed_count += 1
                            # Mark as processed to avoid infinite loops
                            try:
                                self.supabase.table('zuo_3d_embedding').update({'has_asset': True}).eq('Zuo_Item_No', item['zuo_item_no']).execute()
                                self.logger.info(f"Marked {item['zuo_item_no']} as processed to skip in future")
                            except Exception as mark_error:
                                self.logger.error(f"Failed to mark {item['zuo_item_no']} as processed: {mark_error}")
                                
                # Brief pause before next item
                if not test_mode:
                    self.logger.info(f"⏳ Waiting 3 seconds before next item...")
                    time.sleep(3)
                    
            self.logger.info(f"\n🎉 Generation Complete!")
            self.logger.info(f"✅ Success: {success_count}")
            self.logger.info(f"❌ Failed: {failed_count}")
            self.logger.info(f"📊 Total Processed: {processed_count}")
            if processed_count > 0:
                self.logger.info(f"📊 Success Rate: {(success_count / processed_count * 100):.1f}%")
            
        except Exception as e:
            self.logger.error(f"Fatal error: {e}")
            raise

def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Generate 3D models for Zuo furniture using Trellis')
    parser.add_argument('--limit', type=int, help='Limit number of items to process')
    parser.add_argument('--test', action='store_true', help='Test mode - process only 1 item')
    
    args = parser.parse_args()
    
    # Create and run generator
    generator = TrellisGenerator()
    generator.run(limit=args.limit, test_mode=args.test)

if __name__ == "__main__":
    main()
