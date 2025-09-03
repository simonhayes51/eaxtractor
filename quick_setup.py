#!/usr/bin/env python3
"""
EA FC DataMiner Quick Setup & URL Discovery
Run this first to find working endpoints before starting the main monitor
"""

import requests
import json
import re
from pathlib import Path
import time

def test_endpoint(url, timeout=10):
    """Test if an endpoint is accessible and return info"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/html, */*',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        
        result = {
            'url': url,
            'status': response.status_code,
            'content_type': response.headers.get('content-type', ''),
            'size': len(response.content),
            'working': response.status_code == 200,
            'content_preview': ''
        }
        
        if response.status_code == 200:
            content = response.text[:300]
            result['content_preview'] = content.replace('\n', ' ').strip()
        
        return result
        
    except Exception as e:
        return {
            'url': url,
            'status': 'ERROR',
            'error': str(e),
            'working': False
        }

def discover_from_main_page():
    """Discover endpoints from EA FC main web app"""
    discovered_urls = set()
    
    print("🔍 Discovering endpoints from main EA FC web app...")
    
    try:
        result = test_endpoint("https://www.ea.com/fifa/ultimate-team/web-app/")
        if result['working']:
            content = requests.get("https://www.ea.com/fifa/ultimate-team/web-app/").text
            
            # Find JavaScript files
            js_pattern = r'src=["\']([^"\']*\.js[^"\']*)["\']'
            js_files = re.findall(js_pattern, content)
            
            for js_file in js_files:
                if not js_file.startswith('http'):
                    js_file = f"https://www.ea.com{js_file}"
                discovered_urls.add(js_file)
            
            # Find API references
            api_patterns = [
                r'"(https://[^"]*\.ea\.com[^"]*api[^"]*)"',
                r'"(/api/[^"]*)"',
                r'"(https://[^"]*fut[^"]*)"',
            ]
            
            for pattern in api_patterns:
                matches = re.findall(pattern, content)
                for match in matches:
                    if not match.startswith('http'):
                        match = f"https://www.ea.com{match}"
                    discovered_urls.add(match)
            
            print(f"✅ Discovered {len(discovered_urls)} potential URLs")
        else:
            print("❌ Could not access main EA FC web app")
            
    except Exception as e:
        print(f"❌ Error during discovery: {e}")
    
    return discovered_urls

def test_common_patterns():
    """Test common EA FC URL patterns"""
    print("\n🎯 Testing common EA FC endpoint patterns...")
    
    # Base URLs to try
    bases = [
        "https://www.ea.com/fifa/ultimate-team/web-app",
        "https://www.ea.com/fifa/ultimate-team/companion-app",
        "https://www.easports.com/fifa/ultimate-team",
    ]
    
    # Common API endpoints
    endpoints = [
        "",
        "/api/",
        "/api/sbc/",
        "/api/objectives/", 
        "/api/packs/",
        "/api/players/",
        "/api/market/",
        "/api/campaigns/",
        "/config/config.json",
        "/content/campaigns.json",
        "/loc/messages_en.json"
    ]
    
    # CDN patterns
    cdn_bases = [
        "https://fifa24.content.easports.com/fifa/fltOnlineAssets",
        "https://fifa25.content.easports.com/fifa/fltOnlineAssets", 
        "https://media.contentapi.ea.com/content/dam/eacom/fifa",
    ]
    
    test_urls = []
    
    # Combine bases with endpoints
    for base in bases:
        for endpoint in endpoints:
            test_urls.append(f"{base}{endpoint}")
    
    # Add CDN URLs
    test_urls.extend(cdn_bases)
    
    return test_urls

def main():
    print("""
╔══════════════════════════════════════════════════════════════╗
║              EA FC DataMiner - Quick Setup                  ║
║          Endpoint Discovery & Validation Tool               ║
╚══════════════════════════════════════════════════════════════╝
    """)
    
    # Create results directory
    Path("setup_results").mkdir(exist_ok=True)
    
    working_endpoints = []
    
    # Step 1: Discover from main page
    discovered = discover_from_main_page()
    
    # Step 2: Test common patterns  
    common_patterns = test_common_patterns()
    
    # Step 3: Combine and test all URLs
    all_urls = list(discovered) + common_patterns
    all_urls = list(set(all_urls))  # Remove duplicates
    
    print(f"\n🧪 Testing {len(all_urls)} total URLs...")
    print("This may take a few minutes with rate limiting...\n")
    
    results = []
    for i, url in enumerate(all_urls):
        print(f"[{i+1}/{len(all_urls)}] Testing: {url[:60]}...")
        
        result = test_endpoint(url)
        results.append(result)
        
        if result['working']:
            working_endpoints.append(url)
            print(f"  ✅ WORKING ({result['size']} bytes)")
            if 'json' in result['content_type']:
                print("    📊 JSON data detected!")
            elif 'javascript' in result['content_type']:
                print("    🔧 JavaScript file detected!")
        else:
            print(f"  ❌ Failed ({result.get('status', 'ERROR')})")
        
        # Rate limiting
        time.sleep(2)
    
    # Save results
    print(f"\n📊 Results Summary:")
    print(f"  • Total URLs tested: {len(all_urls)}")
    print(f"  • Working endpoints: {len(working_endpoints)}")
    print(f"  • Success rate: {len(working_endpoints)/len(all_urls)*100:.1f}%")
    
    # Save working endpoints to file
    with open("setup_results/working_endpoints.json", "w") as f:
        json.dump(working_endpoints, f, indent=2)
    
    # Save detailed results
    with open("setup_results/detailed_results.json", "w") as f:
        json.dump(results, f, indent=2)
    
    # Generate endpoints for main script
    endpoints_config = {}
    for i, url in enumerate(working_endpoints):
        # Create clean names for endpoints
        if 'api' in url:
            name = f"api_{i}"
        elif '.js' in url:
            name = f"js_{i}"
        elif '.json' in url:
            name = f"config_{i}"
        else:
            name = f"endpoint_{i}"
        
        endpoints_config[name] = url
    
    with open("setup_results/endpoints_config.py", "w") as f:
        f.write("# Add these to your main monitoring script\n")
        f.write("# Replace the endpoints dictionary with this:\n\n")
        f.write("endpoints = {\n")
        for name, url in endpoints_config.items():
            f.write(f'    "{name}": "{url}",\n')
        f.write("}\n")
    
    print(f"\n💾 Results saved to setup_results/")
    print(f"  • working_endpoints.json - List of working URLs")
    print(f"  • detailed_results.json - Full test results")
    print(f"  • endpoints_config.py - Ready to use in main script")
    
    # Show top working endpoints
    print(f"\n🎯 Top Working Endpoints Found:")
    for url in working_endpoints[:10]:
        print(f"  • {url}")
    
    if len(working_endpoints) > 10:
        print(f"  ... and {len(working_endpoints)-10} more")
    
    print(f"\n🚀 Next Steps:")
    print(f"  1. Review setup_results/working_endpoints.json")
    print(f"  2. Copy endpoints from endpoints_config.py to main script")
    print(f"  3. Run the main EA FC DataMiner script")
    print(f"  4. Monitor the results/ directory for changes")

if __name__ == "__main__":
    main()
