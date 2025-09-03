#!/usr/bin/env python3
"""
EA FC Endpoint Finder - Run this locally to find real EA FC URLs
Then update your Railway deployment with the working endpoints
"""

import requests
import re
import json
from urllib.parse import urljoin, urlparse

def find_ea_fc_endpoints():
    """Find real EA FC endpoints by analyzing the web app"""
    
    print("🔍 Analyzing EA FC Web App for API endpoints...")
    
    base_url = "https://www.ea.com/fifa/ultimate-team/web-app/"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
    }
    
    discovered_endpoints = {}
    
    try:
        # Get main page
        response = requests.get(base_url, headers=headers, timeout=10)
        if response.status_code != 200:
            print(f"❌ Could not access EA FC web app: {response.status_code}")
            return {}
        
        content = response.text
        print("✅ Successfully loaded EA FC web app")
        
        # Find JavaScript files
        js_pattern = r'src=["\']([^"\']+\.js[^"\']*)["\']'
        js_files = re.findall(js_pattern, content)
        
        print(f"📄 Found {len(js_files)} JavaScript files")
        
        # Analyze JavaScript files for API endpoints
        api_endpoints = set()
        
        for js_file in js_files[:5]:  # Check first 5 JS files
            if not js_file.startswith('http'):
                js_url = urljoin(base_url, js_file)
            else:
                js_url = js_file
            
            try:
                print(f"📁 Analyzing: {js_url}")
                js_response = requests.get(js_url, headers=headers, timeout=10)
                if js_response.status_code == 200:
                    js_content = js_response.text
                    
                    # Find API patterns in JavaScript
                    api_patterns = [
                        r'"(/api/[^"]+)"',
                        r"'(/api/[^']+)'",
                        r'"(https://[^"]*api[^"]*)"',
                        r"'(https://[^']*api[^']*)'",
                        r'"(/fut[^"]*)"',
                        r'"(https://[^"]*fut[^"]*)"'
                    ]
                    
                    for pattern in api_patterns:
                        matches = re.findall(pattern, js_content)
                        for match in matches:
                            if match.startswith('/'):
                                api_endpoints.add(urljoin(base_url, match))
                            else:
                                api_endpoints.add(match)
            
            except Exception as e:
                print(f"⚠️ Error analyzing {js_url}: {e}")
        
        # Test discovered endpoints
        print(f"\n🧪 Testing {len(api_endpoints)} discovered endpoints...")
        
        working_endpoints = {}
        
        for i, endpoint in enumerate(api_endpoints):
            if i >= 20:  # Limit testing to avoid spam
                break
                
            try:
                test_response = requests.head(endpoint, headers=headers, timeout=5)
                status = test_response.status_code
                
                endpoint_name = f"api_{i}"
                if 'sbc' in endpoint.lower():
                    endpoint_name = f"sbc_api_{i}"
                elif 'objective' in endpoint.lower():
                    endpoint_name = f"objectives_api_{i}"
                elif 'pack' in endpoint.lower():
                    endpoint_name = f"packs_api_{i}"
                elif 'player' in endpoint.lower():
                    endpoint_name = f"players_api_{i}"
                
                if status in [200, 405, 403]:  # These indicate the endpoint exists
                    working_endpoints[endpoint_name] = endpoint
                    print(f"✅ {endpoint_name}: {endpoint} (HTTP {status})")
                else:
                    print(f"❌ {endpoint}: HTTP {status}")
                    
            except Exception as e:
                print(f"❌ {endpoint}: Error - {e}")
        
        # Add some known working patterns to test
        test_patterns = [
            "https://www.ea.com/fifa/ultimate-team/web-app/config/config.json",
            "https://www.ea.com/fifa/ultimate-team/web-app/loc/messages_en.json",
            "https://www.ea.com/fifa/ultimate-team/web-app/content/",
            "https://www.easports.com/fifa/ultimate-team/web-app/",
            "https://fifa25.content.easports.com/fifa/fltOnlineAssets/",  # Try current version
            "https://media.contentapi.ea.com/content/dam/eacom/fifa/",
        ]
        
        print(f"\n🎯 Testing known EA FC patterns...")
        
        for pattern in test_patterns:
            try:
                test_response = requests.head(pattern, headers=headers, timeout=5)
                if test_response.status_code in [200, 403, 405]:
                    name = f"known_{len(working_endpoints)}"
                    if 'config' in pattern:
                        name = 'config_file'
                    elif 'messages' in pattern:
                        name = 'localization'
                    elif 'content' in pattern:
                        name = 'content_cdn'
                    
                    working_endpoints[name] = pattern
                    print(f"✅ {name}: {pattern} (HTTP {test_response.status_code})")
            except:
                pass
        
        discovered_endpoints = working_endpoints
        
    except Exception as e:
        print(f"❌ Error analyzing EA FC web app: {e}")
    
    return discovered_endpoints

def generate_railway_config(endpoints):
    """Generate configuration for Railway deployment"""
    
    if not endpoints:
        print("❌ No working endpoints found")
        return
    
    print(f"\n📝 Generating Railway configuration...")
    print(f"Found {len(endpoints)} working endpoints\n")
    
    # Generate Python code for Railway script
    config_code = "# Replace the endpoints dictionary in railway_main.py with this:\n\n"
    config_code += "self.endpoints = {\n"
    
    for name, url in endpoints.items():
        config_code += f'    "{name}": "{url}",\n'
    
    config_code += "}\n"
    
    print("📋 Copy this into your railway_main.py file:")
    print("="*60)
    print(config_code)
    print("="*60)
    
    # Save to file
    with open("railway_endpoints.py", "w") as f:
        f.write(config_code)
    
    print(f"\n💾 Configuration saved to: railway_endpoints.py")
    print(f"🚀 Update your Railway deployment with these endpoints for better monitoring!")

def main():
    print("""
╔══════════════════════════════════════════════════════════════╗
║                EA FC Endpoint Finder                        ║
║          Discover Real EA FC API URLs                       ║
╚══════════════════════════════════════════════════════════════╝
    """)
    
    endpoints = find_ea_fc_endpoints()
    generate_railway_config(endpoints)
    
    if endpoints:
        print(f"\n✅ Success! Found {len(endpoints)} working endpoints")
        print("📝 Next steps:")
        print("1. Copy the endpoints configuration above")
        print("2. Update your railway_main.py file")
        print("3. Redeploy on Railway")
        print("4. Check logs to see improved monitoring")
    else:
        print("\n❌ No endpoints found. EA FC structure may have changed.")
        print("💡 Try manually browsing EA FC web app with browser dev tools")

if __name__ == "__main__":
    main()
