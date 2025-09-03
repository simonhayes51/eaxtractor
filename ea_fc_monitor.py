import requests
import hashlib
import json
import time
import os
import csv
from datetime import datetime, timedelta
import asyncio
import aiohttp
from pathlib import Path
import logging
import re
from typing import Dict, List, Optional, Set
import sqlite3

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('ea_fc_monitor.log'),
        logging.StreamHandler()
    ]
)

class EAFCDataMiner:
    def __init__(self, check_interval=1200):  # 20 minutes default
        self.check_interval = check_interval
        self.known_hashes = {}
        self.session = None
        self.results_dir = Path("results")
        self.changes_dir = Path("changes")
        self.exports_dir = Path("exports")
        self.db_path = Path("data/ea_fc_changes.db")
        
        # Create directories
        for dir_path in [self.results_dir, self.changes_dir, self.exports_dir, Path("data")]:
            dir_path.mkdir(exist_ok=True)
        
        # Initialize database
        self.init_database()
        
        # Specific EA FC endpoints (these are the key ones to find)
        self.endpoints = {
            # Main web app files
            "web_app_main": "https://www.ea.com/fifa/ultimate-team/web-app/",
            "web_app_config": "https://www.ea.com/fifa/ultimate-team/web-app/config/config.json",
            
            # API endpoints (you'll need to discover the exact URLs)
            "fut_web_api": "https://www.ea.com/fifa/ultimate-team/web-app/api/",
            "companion_api": "https://www.ea.com/fifa/ultimate-team/companion-app/",
            
            # Content delivery
            "static_content": "https://media.contentapi.ea.com/content/dam/eacom/fifa/",
            "cdn_assets": "https://fifa23.content.easports.com/fifa/fltOnlineAssets/",  # Update version
            
            # Potential API patterns to test
            "sbc_endpoint": "https://www.ea.com/fifa/ultimate-team/api/sbc",
            "objectives_endpoint": "https://www.ea.com/fifa/ultimate-team/api/objectives",
            "packs_endpoint": "https://www.ea.com/fifa/ultimate-team/api/packs",
            "players_endpoint": "https://www.ea.com/fifa/ultimate-team/api/players",
            "market_endpoint": "https://www.ea.com/fifa/ultimate-team/api/market",
        }
        
        # Enhanced keyword detection
        self.content_patterns = {
            'sbc_indicators': [
                r'"challengeName":\s*"([^"]+)"',
                r'"sbc[^"]*":\s*"([^"]+)"',
                r'"requirements":\s*\{[^}]*"rating":\s*(\d+)',
                r'"chemistry":\s*(\d+)',
                r'"reward":\s*"([^"]+)"',
                r'squad.*building.*challenge',
                r'completion.*reward'
            ],
            'promo_indicators': [
                r'"promoName":\s*"([^"]+)"',
                r'"campaignName":\s*"([^"]+)"',
                r'"eventName":\s*"([^"]+)"',
                r'totw|team.*week',
                r'toty|team.*year',
                r'tots|team.*season',
                r'fut.*champions',
                r'weekend.*league'
            ],
            'player_indicators': [
                r'"displayName":\s*"([^"]+)"',
                r'"playerName":\s*"([^"]+)"',
                r'"rating":\s*(\d{2,3})',
                r'"position":\s*"([A-Z]{2,3})"',
                r'"nation":\s*"([^"]+)"',
                r'"club":\s*"([^"]+)"'
            ],
            'pack_indicators': [
                r'"packName":\s*"([^"]+)"',
                r'"packType":\s*"([^"]+)"',
                r'"packPrice":\s*(\d+)',
                r'"packOdds":\s*([0-9.]+)',
                r'lightning.*round',
                r'player.*pick'
            ]
        }

    def init_database(self):
        """Initialize SQLite database for change tracking"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    change_type TEXT,
                    significance_score INTEGER,
                    content_hash TEXT,
                    extracted_data TEXT,
                    filename TEXT
                )
            ''')
            
            conn.execute('''
                CREATE TABLE IF NOT EXISTS discovered_content (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    details TEXT,
                    endpoint TEXT,
                    confidence_score INTEGER
                )
            ''')

    async def initialize_session(self):
        """Initialize aiohttp session with EA-friendly headers"""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'DNT': '1',
            'Pragma': 'no-cache',
            'Cache-Control': 'no-cache'
        }
        
        timeout = aiohttp.ClientTimeout(total=45)
        connector = aiohttp.TCPConnector(
            limit=3,  # Very conservative
            ttl_dns_cache=300,
            use_dns_cache=True,
        )
        
        self.session = aiohttp.ClientSession(
            headers=headers,
            timeout=timeout,
            connector=connector
        )

    def load_known_hashes(self, filename="data/known_hashes.json"):
        """Load previously recorded file hashes"""
        try:
            with open(filename, 'r') as f:
                self.known_hashes = json.load(f)
                logging.info(f"Loaded {len(self.known_hashes)} known hashes")
        except FileNotFoundError:
            self.known_hashes = {}
            logging.info("No previous hashes found, starting fresh")

    def save_known_hashes(self, filename="data/known_hashes.json"):
        """Save current file hashes"""
        with open(filename, 'w') as f:
            json.dump(self.known_hashes, f, indent=2)

    def get_file_hash(self, content: str) -> str:
        """Generate SHA256 hash of content"""
        return hashlib.sha256(content.encode('utf-8')).hexdigest()

    async def check_endpoint(self, name: str, url: str) -> Optional[Dict]:
        """Check a single endpoint for changes with enhanced error handling"""
        try:
            # Rate limiting - wait between requests
            await asyncio.sleep(3)
            
            async with self.session.get(url) as response:
                if response.status == 200:
                    content = await response.text()
                    current_hash = self.get_file_hash(content)
                    
                    if name not in self.known_hashes:
                        self.known_hashes[name] = current_hash
                        logging.info(f"✅ New endpoint tracked: {name}")
                        # Save initial content for comparison
                        await self.save_initial_content(name, content)
                        return None
                    
                    if self.known_hashes[name] != current_hash:
                        logging.warning(f"🚨 CHANGE DETECTED: {name}")
                        change_data = await self.process_change(name, url, content)
                        self.known_hashes[name] = current_hash
                        return change_data
                        
                elif response.status == 404:
                    logging.warning(f"❌ Endpoint not found: {name}")
                elif response.status == 403:
                    logging.warning(f"🔒 Access denied: {name}")
                elif response.status == 429:
                    logging.warning(f"⏰ Rate limited: {name} - increasing delay")
                    await asyncio.sleep(30)  # Back off on rate limiting
                else:
                    logging.warning(f"⚠️ HTTP {response.status} for {name}")
                    
        except asyncio.TimeoutError:
            logging.error(f"⏱️ Timeout checking {name}")
        except Exception as e:
            logging.error(f"💥 Error checking {name}: {e}")
        
        return None

    async def save_initial_content(self, name: str, content: str):
        """Save initial content for new endpoints"""
        timestamp = datetime.now()
        filename = f"initial_{timestamp.strftime('%Y%m%d_%H%M%S')}_{name}.txt"
        filepath = self.changes_dir / filename
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(f"Initial Content - {name}\n")
            f.write(f"Timestamp: {timestamp}\n")
            f.write("-" * 80 + "\n")
            f.write(content)

    async def process_change(self, name: str, url: str, content: str) -> Dict:
        """Process detected changes with advanced analysis"""
        timestamp = datetime.now()
        
        # Save raw content with better naming
        safe_name = re.sub(r'[^\w\-_]', '_', name)
        filename = f"{timestamp.strftime('%Y%m%d_%H%M%S')}_{safe_name}.txt"
        filepath = self.changes_dir / filename
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(f"🔄 CONTENT CHANGE DETECTED\n")
            f.write(f"Endpoint: {name}\n")
            f.write(f"URL: {url}\n")
            f.write(f"Timestamp: {timestamp}\n")
            f.write(f"Content Length: {len(content)} characters\n")
            f.write("=" * 80 + "\n")
            f.write(content)

        # Enhanced content analysis
        analysis = await self.analyze_content_advanced(content)
        
        change_data = {
            'timestamp': timestamp.isoformat(),
            'endpoint': name,
            'url': url,
            'filename': filename,
            'analysis': analysis,
            'content_length': len(content),
            'content_preview': content[:500] + "..." if len(content) > 500 else content
        }
        
        # Save to database
        await self.save_to_database(change_data)
        
        # Generate immediate alert if high significance
        if analysis['significance_score'] > 10:
            await self.generate_alert(change_data)
        
        return change_data

    async def analyze_content_advanced(self, content: str) -> Dict:
        """Advanced content analysis with pattern matching"""
        analysis = {
            'found_sbcs': [],
            'found_promos': [],
            'found_players': [],
            'found_packs': [],
            'significance_score': 0,
            'change_type': 'unknown',
            'confidence': 0
        }
        
        # Pattern matching for each content type
        for category, patterns in self.content_patterns.items():
            matches = []
            for pattern in patterns:
                found = re.findall(pattern, content, re.IGNORECASE)
                matches.extend(found)
            
            if category == 'sbc_indicators':
                analysis['found_sbcs'] = list(set(matches))
                analysis['significance_score'] += len(matches) * 5
            elif category == 'promo_indicators':
                analysis['found_promos'] = list(set(matches))
                analysis['significance_score'] += len(matches) * 8
            elif category == 'player_indicators':
                analysis['found_players'] = list(set(matches))
                analysis['significance_score'] += len(matches) * 2
            elif category == 'pack_indicators':
                analysis['found_packs'] = list(set(matches))
                analysis['significance_score'] += len(matches) * 3
        
        # Determine change type and confidence
        if analysis['found_sbcs']:
            analysis['change_type'] = 'sbc_update'
            analysis['confidence'] = 85
        elif analysis['found_promos']:
            analysis['change_type'] = 'promo_update'
            analysis['confidence'] = 90
        elif analysis['found_packs']:
            analysis['change_type'] = 'pack_update'
            analysis['confidence'] = 75
        elif analysis['found_players']:
            analysis['change_type'] = 'player_update'
            analysis['confidence'] = 60
        
        # Look for specific high-value indicators
        high_value_terms = [
            'toty', 'team of the year',
            'tots', 'team of the season', 
            'fut champions', 'weekend league',
            'icon', 'hero', 'flashback'
        ]
        
        content_lower = content.lower()
        for term in high_value_terms:
            if term in content_lower:
                analysis['significance_score'] += 15
                analysis['confidence'] = min(95, analysis['confidence'] + 10)
        
        return analysis

    async def save_to_database(self, change_data: Dict):
        """Save change data to SQLite database"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT INTO changes 
                (timestamp, endpoint, change_type, significance_score, content_hash, extracted_data, filename)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                change_data['timestamp'],
                change_data['endpoint'],
                change_data['analysis']['change_type'],
                change_data['analysis']['significance_score'],
                self.get_file_hash(change_data['content_preview']),
                json.dumps(change_data['analysis']),
                change_data['filename']
            ))
            
            # Save discovered content items
            analysis = change_data['analysis']
            for sbc in analysis['found_sbcs'][:5]:  # Limit to top 5
                conn.execute('''
                    INSERT INTO discovered_content 
                    (timestamp, content_type, name, details, endpoint, confidence_score)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    change_data['timestamp'],
                    'SBC',
                    str(sbc),
                    json.dumps({'type': 'sbc', 'source': change_data['endpoint']}),
                    change_data['endpoint'],
                    analysis['confidence']
                ))

    async def generate_alert(self, change_data: Dict):
        """Generate high-priority alert for significant changes"""
        alert_file = self.exports_dir / f"ALERT_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        
        analysis = change_data['analysis']
        
        alert_content = f"""# 🚨 HIGH PRIORITY EA FC ALERT
        
**Timestamp**: {change_data['timestamp']}
**Endpoint**: {change_data['endpoint']}
**Significance Score**: {analysis['significance_score']} 
**Change Type**: {analysis['change_type']}
**Confidence**: {analysis['confidence']}%

## Detected Content

"""
        
        if analysis['found_sbcs']:
            alert_content += f"### 🏆 SBCs Found ({len(analysis['found_sbcs'])})\n"
            for sbc in analysis['found_sbcs'][:10]:
                alert_content += f"- {sbc}\n"
            alert_content += "\n"
        
        if analysis['found_promos']:
            alert_content += f"### 🎉 Promos Found ({len(analysis['found_promos'])})\n"
            for promo in analysis['found_promos'][:10]:
                alert_content += f"- {promo}\n"
            alert_content += "\n"
        
        if analysis['found_packs']:
            alert_content += f"### 📦 Packs Found ({len(analysis['found_packs'])})\n"
            for pack in analysis['found_packs'][:10]:
                alert_content += f"- {pack}\n"
            alert_content += "\n"
        
        alert_content += f"\n**Raw File**: `{change_data['filename']}`\n"
        
        with open(alert_file, 'w', encoding='utf-8') as f:
            f.write(alert_content)
        
        logging.warning(f"🚨 HIGH PRIORITY ALERT GENERATED: {alert_file}")

    def export_comprehensive_report(self):
        """Export comprehensive daily report with database queries"""
        today = datetime.now().date()
        report_file = self.exports_dir / f"comprehensive_report_{today}.md"
        
        with sqlite3.connect(self.db_path) as conn:
            # Get today's changes
            today_changes = conn.execute('''
                SELECT * FROM changes 
                WHERE date(timestamp) = date('now')
                ORDER BY significance_score DESC
            ''').fetchall()
            
            # Get discovered content
            discovered_content = conn.execute('''
                SELECT content_type, name, confidence_score, COUNT(*) as frequency
                FROM discovered_content 
                WHERE date(timestamp) = date('now')
                GROUP BY content_type, name
                ORDER BY confidence_score DESC, frequency DESC
            ''').fetchall()
        
        # Generate comprehensive report
        report = f"""# EA FC Comprehensive Daily Report - {today}

## Summary
- **Total Changes**: {len(today_changes)}
- **High Significance Changes**: {len([c for c in today_changes if c[4] > 10])}
- **Unique Content Items**: {len(discovered_content)}

"""
        
        # High significance changes
        high_sig = [c for c in today_changes if c[4] > 10]
        if high_sig:
            report += f"## 🚨 High Significance Changes ({len(high_sig)})\n\n"
            for change in high_sig:
                report += f"### {change[2]} - Score: {change[4]}\n"
                report += f"**Endpoint**: {change[1]}\n"
                report += f"**Time**: {change[0]}\n"
                report += f"**Type**: {change[3]}\n\n"
        
        # Discovered content summary
        if discovered_content:
            report += "## 📋 Discovered Content\n\n"
            
            current_type = None
            for item in discovered_content:
                content_type, name, confidence, frequency = item
                if content_type != current_type:
                    report += f"### {content_type}s\n"
                    current_type = content_type
                report += f"- **{name}** (Confidence: {confidence}%, Seen: {frequency}x)\n"
            report += "\n"
        
        # Export CSV for analysis
        csv_file = self.exports_dir / f"changes_data_{today}.csv"
        with open(csv_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['timestamp', 'endpoint', 'change_type', 'significance_score', 'filename'])
            for change in today_changes:
                writer.writerow([change[0], change[1], change[3], change[4], change[7]])
        
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(report)
        
        logging.info(f"📊 Comprehensive report exported: {report_file}")

    async def discover_endpoints_advanced(self):
        """Advanced endpoint discovery with multiple techniques"""
        discovered = set()
        
        try:
            # Method 1: Analyze main web app
            async with self.session.get(self.endpoints["web_app_main"]) as response:
                if response.status == 200:
                    content = await response.text()
                    
                    # Find JavaScript files
                    js_files = re.findall(r'src=["\']([^"\']*\.js[^"\']*)["\']', content)
                    for js_file in js_files[:10]:  # Limit to avoid spam
                        if not js_file.startswith('http'):
                            js_file = f"https://www.ea.com{js_file}"
                        discovered.add(js_file)
                    
                    # Find API patterns
                    api_patterns = [
                        r'"(https://[^"]*\.ea\.com[^"]*api[^"]*)"',
                        r'"(/api/[^"]*)"',
                        r'"(https://[^"]*fut[^"]*)"',
                        r'"(https://[^"]*ultimate-team[^"]*)"'
                    ]
                    
                    for pattern in api_patterns:
                        matches = re.findall(pattern, content)
                        for match in matches:
                            if not match.startswith('http'):
                                match = f"https://www.ea.com{match}"
                            discovered.add(match)
            
            # Method 2: Try common EA FC API patterns
            common_patterns = [
                "https://www.ea.com/fifa/ultimate-team/web-app/content/",
                "https://www.ea.com/fifa/ultimate-team/web-app/loc/",
                "https://www.ea.com/fifa/ultimate-team/web-app/api/sbc/",
                "https://www.ea.com/fifa/ultimate-team/web-app/api/objectives/",
                "https://www.easports.com/fifa/ultimate-team/api/",
                "https://fifa23.content.easports.com/fifa/fltOnlineAssets/",  # Update version
            ]
            
            for pattern in common_patterns:
                discovered.add(pattern)
        
        except Exception as e:
            logging.error(f"Error in endpoint discovery: {e}")
        
        # Add discovered endpoints
        new_count = 0
        for url in discovered:
            if url not in self.endpoints.values() and len(url) > 10:
                endpoint_name = f"discovered_{new_count}"
                self.endpoints[endpoint_name] = url
                new_count += 1
                logging.info(f"🔍 Added discovered endpoint: {url}")
        
        logging.info(f"🔍 Discovery complete: {new_count} new endpoints added")

    async def run_monitoring_cycle(self):
        """Run one complete monitoring cycle"""
        cycle_start = time.time()
        logging.info(f"🔄 Starting monitoring cycle - {len(self.endpoints)} endpoints")
        
        changes_detected = []
        
        for name, url in self.endpoints.items():
            try:
                change_data = await self.check_endpoint(name, url)
                if change_data:
                    changes_detected.append(change_data)
            except Exception as e:
                logging.error(f"💥 Error in cycle for {name}: {e}")
        
        cycle_duration = time.time() - cycle_start
        
        if changes_detected:
            logging.warning(f"🎯 {len(changes_detected)} changes detected in {cycle_duration:.1f}s!")
            
            # Count high significance changes  
            high_sig = [c for c in changes_detected if c['analysis']['significance_score'] > 10]
            if high_sig:
                logging.warning(f"🚨🚨 {len(high_sig)} HIGH SIGNIFICANCE changes! 🚨🚨")
        else:
            logging.info(f"✅ No changes detected ({cycle_duration:.1f}s scan)")
        
        self.save_known_hashes()
        return changes_detected

    async def start_monitoring(self):
        """Start the main monitoring loop"""
        await self.initialize_session()
        self.load_known_hashes()
        
        logging.info("🚀 EA FC DataMiner v2.0 Starting")
        logging.info(f"⏰ Check interval: {self.check_interval//60} minutes")
        logging.info(f"🎯 Monitoring {len(self.endpoints)} endpoints")
        
        # Initial endpoint discovery
        logging.info("🔍 Running endpoint discovery...")
        await self.discover_endpoints_advanced()
        
        try:
            cycle_count = 0
            while True:
                cycle_count += 1
                logging.info(f"🔄 Cycle {cycle_count} starting...")
                
                cycle_start = time.time()
                changes = await self.run_monitoring_cycle()
                
                # Export reports
                if changes or cycle_count % 6 == 0:  # Every 6 cycles or when changes found
                    self.export_comprehensive_report()
                
                # Calculate next check time
                cycle_duration = time.time() - cycle_start
                sleep_time = max(60, self.check_interval - cycle_duration)  # Minimum 1 minute
                
                next_check = datetime.now() + timedelta(seconds=sleep_time)
                logging.info(f"😴 Next check at {next_check.strftime('%H:%M:%S')} (in {sleep_time//60:.0f}m {sleep_time%60:.0f}s)")
                
                await asyncio.sleep(sleep_time)
                
        except KeyboardInterrupt:
            logging.info("🛑 Monitoring stopped by user")
        except Exception as e:
            logging.error(f"💥 Fatal error: {e}")
        finally:
            if self.session:
                await self.session.close()
            logging.info("🏁 EA FC DataMiner stopped")

# Main execution
async def main():
    print("""
╔══════════════════════════════════════════════════════════════╗
║                    EA FC DataMiner v2.0                     ║
║                  Advanced Content Detection                  ║
╠══════════════════════════════════════════════════════════════╣
║  • 20-minute monitoring intervals with rate limiting        ║
║  • Advanced pattern matching for SBCs, promos, players      ║
║  • SQLite database for historical tracking                  ║
║  • Comprehensive daily reports and CSV exports              ║
║  • Real-time alerts for high-significance changes           ║
╚══════════════════════════════════════════════════════════════╝
    """)
    
    miner = EAFCDataMiner(check_interval=1200)  # 20 minutes
    await miner.start_monitoring()

if __name__ == "__main__":
    asyncio.run(main())
