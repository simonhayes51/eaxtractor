#!/usr/bin/env python3
"""
EA FC DataMiner - Railway Deployment Version
Optimized for cloud deployment with health checks and environment variables
"""

import os
import asyncio
import aiohttp
import json
import hashlib
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
import re
from typing import Dict, List, Optional
import signal
import sys
from aiohttp import web
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging for Railway
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

class RailwayEAFCDataMiner:
    def __init__(self):
        # Railway environment variables
        self.check_interval = int(os.getenv('CHECK_INTERVAL', 1200))  # 20 minutes
        self.discord_webhook = os.getenv('DISCORD_WEBHOOK_URL')
        self.port = int(os.getenv('PORT', 8080))
        
        # Thresholds
        self.high_threshold = int(os.getenv('HIGH_PRIORITY_THRESHOLD', 15))
        self.medium_threshold = int(os.getenv('MEDIUM_PRIORITY_THRESHOLD', 8))
        self.low_threshold = int(os.getenv('LOW_PRIORITY_THRESHOLD', 3))
        
        # Data storage (Railway has ephemeral filesystem, so we'll use in-memory with periodic saves)
        self.known_hashes = {}
        self.changes_log = []
        self.session = None
        self.running = False
        
        # Create minimal directory structure
        Path("data").mkdir(exist_ok=True)
        Path("logs").mkdir(exist_ok=True)
        
        # Initialize database
        self.db_path = "data/ea_fc_changes.db"
        self.init_database()
        
        # EA FC endpoints - optimized list for Railway
        self.endpoints = {
            "web_app_main": "https://www.ea.com/fifa/ultimate-team/web-app/",
            "web_app_config": "https://www.ea.com/fifa/ultimate-team/web-app/config/config.json",
            "sbc_endpoint": "https://www.ea.com/fifa/ultimate-team/api/sbc",
            "objectives_endpoint": "https://www.ea.com/fifa/ultimate-team/api/objectives", 
            "packs_endpoint": "https://www.ea.com/fifa/ultimate-team/api/packs",
            "players_endpoint": "https://www.ea.com/fifa/ultimate-team/api/players",
            "static_content": "https://media.contentapi.ea.com/content/dam/eacom/fifa/",
            "cdn_assets": "https://fifa24.content.easports.com/fifa/fltOnlineAssets/"
        }
        
        # Enhanced content patterns for FC 25 API responses
        self.content_patterns = {
            'sbc_indicators': [
                r'"challengeName":\s*"([^"]+)"',
                r'"name":\s*"([^"]+)"',
                r'"setName":\s*"([^"]+)"',
                r'"requirements":\s*\{[^}]*"rating":\s*(\d+)',
                r'"chemistry":\s*(\d+)',
                r'"challengeId":\s*(\d+)',
                r'"setId":\s*(\d+)',
                r'squad.*building.*challenge',
            ],
            'promo_indicators': [
                r'"promoName":\s*"([^"]+)"',
                r'"campaignName":\s*"([^"]+)"',
                r'"eventName":\s*"([^"]+)"',
                r'"featureConsumerId":\s*"([^"]+)"',
                r'totw|team.*week',
                r'toty|team.*year',
                r'tots|team.*season',
                r'fut.*champions',
                r'weekend.*league',
                r'flashback|moments|hero|icon',
            ],
            'player_indicators': [
                r'"displayName":\s*"([^"]+)"',
                r'"playerName":\s*"([^"]+)"',
                r'"firstName":\s*"([^"]+)"',
                r'"lastName":\s*"([^"]+)"',
                r'"rating":\s*(\d{2,3})',
                r'"position":\s*"([A-Z]{2,3})"',
                r'"nation":\s*(\d+)',
                r'"club":\s*(\d+)',
            ],
            'pack_indicators': [
                r'"packName":\s*"([^"]+)"',
                r'"packType":\s*"([^"]+)"',
                r'"purchaseGroup":\s*"([^"]+)"',
                r'"categoryName":\s*"([^"]+)"',
                r'"price":\s*(\d+)',
                r'"coins":\s*(\d+)',
                r'"points":\s*(\d+)',
                r'lightning.*round',
                r'player.*pick',
                r'premium.*pack',
            ],
            'objective_indicators': [
                r'"objectiveName":\s*"([^"]+)"',
                r'"description":\s*"([^"]+)"',
                r'"category":\s*"([^"]+)"',
                r'"progress":\s*(\d+)',
                r'"target":\s*(\d+)',
                r'"reward":\s*"([^"]+)"',
                r'daily.*objective',
                r'weekly.*objective',
                r'season.*objective',
            ]
        }
        
        logging.info("🚂 Railway EA FC DataMiner initialized")

    def init_database(self):
        """Initialize SQLite database"""
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
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            conn.execute('''
                CREATE TABLE IF NOT EXISTS discovered_content (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    confidence_score INTEGER,
                    endpoint TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')

    async def initialize_session(self):
        """Initialize aiohttp session"""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Connection': 'keep-alive',
            'DNT': '1'
        }
        
        timeout = aiohttp.ClientTimeout(total=30)
        connector = aiohttp.TCPConnector(limit=3, ttl_dns_cache=300)
        
        self.session = aiohttp.ClientSession(
            headers=headers,
            timeout=timeout,
            connector=connector
        )

    def get_file_hash(self, content: str) -> str:
        """Generate SHA256 hash of content"""
        return hashlib.sha256(content.encode('utf-8')).hexdigest()

    async def check_endpoint(self, name: str, url: str) -> Optional[Dict]:
        """Check endpoint for changes"""
        try:
            await asyncio.sleep(2)  # Rate limiting
            
            async with self.session.get(url) as response:
                if response.status == 200:
                    content = await response.text()
                    current_hash = self.get_file_hash(content)
                    
                    # Check if we've seen this endpoint before
                    if name not in self.known_hashes:
                        self.known_hashes[name] = current_hash
                        logging.info(f"✅ New endpoint tracked: {name}")
                        return None
                    
                    # Check for changes
                    if self.known_hashes[name] != current_hash:
                        logging.warning(f"🚨 CHANGE DETECTED: {name}")
                        change_data = await self.process_change(name, url, content)
                        self.known_hashes[name] = current_hash
                        return change_data
                        
                elif response.status == 429:
                    logging.warning(f"⏰ Rate limited: {name}")
                    await asyncio.sleep(30)
                else:
                    logging.warning(f"⚠️ HTTP {response.status} for {name}")
                    
        except Exception as e:
            logging.error(f"💥 Error checking {name}: {e}")
        
        return None

    async def process_change(self, name: str, url: str, content: str) -> Dict:
        """Process detected change"""
        timestamp = datetime.now()
        
        # Analyze content
        analysis = await self.analyze_content(content)
        
        change_data = {
            'timestamp': timestamp.isoformat(),
            'endpoint': name,
            'url': url,
            'analysis': analysis,
            'content_length': len(content)
        }
        
        # Save to database
        await self.save_to_database(change_data)
        
        # Send notification if significant
        if analysis['significance_score'] > self.low_threshold:
            await self.send_discord_notification(change_data)
        
        # Keep in-memory log
        self.changes_log.append(change_data)
        if len(self.changes_log) > 100:  # Keep only last 100 changes
            self.changes_log.pop(0)
        
        return change_data

    async def analyze_content(self, content: str) -> Dict:
        """Analyze content for EA FC patterns"""
        analysis = {
            'found_sbcs': [],
            'found_promos': [],
            'found_players': [],
            'found_packs': [],
            'significance_score': 0,
            'change_type': 'unknown',
            'confidence': 50
        }
        
        # Pattern matching
        for category, patterns in self.content_patterns.items():
            matches = []
            for pattern in patterns:
                found = re.findall(pattern, content, re.IGNORECASE)
                matches.extend(found)
            
            if category == 'sbc_indicators':
                analysis['found_sbcs'] = list(set(matches))
                analysis['significance_score'] += len(matches) * 5
                if matches:
                    analysis['change_type'] = 'sbc_update'
                    analysis['confidence'] = 85
                    
            elif category == 'promo_indicators':
                analysis['found_promos'] = list(set(matches))
                analysis['significance_score'] += len(matches) * 8
                if matches:
                    analysis['change_type'] = 'promo_update'
                    analysis['confidence'] = 90
                    
            elif category == 'player_indicators':
                analysis['found_players'] = list(set(matches))
                analysis['significance_score'] += len(matches) * 2
                if matches:
                    analysis['change_type'] = 'player_update'
                    analysis['confidence'] = 60
                    
            elif category == 'pack_indicators':
                analysis['found_packs'] = list(set(matches))
                analysis['significance_score'] += len(matches) * 3
                if matches:
                    analysis['change_type'] = 'pack_update'
                    analysis['confidence'] = 75
        
        # High-value terms boost
        high_value_terms = ['toty', 'tots', 'icon', 'hero', 'flashback', 'fut champions']
        content_lower = content.lower()
        for term in high_value_terms:
            if term in content_lower:
                analysis['significance_score'] += 10
                analysis['confidence'] = min(95, analysis['confidence'] + 10)
        
        return analysis

    async def save_to_database(self, change_data: Dict):
        """Save change to database"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    INSERT INTO changes 
                    (timestamp, endpoint, change_type, significance_score, content_hash, extracted_data)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    change_data['timestamp'],
                    change_data['endpoint'],
                    change_data['analysis']['change_type'],
                    change_data['analysis']['significance_score'],
                    self.get_file_hash(str(change_data['analysis'])),
                    json.dumps(change_data['analysis'])
                ))
        except Exception as e:
            logging.error(f"Database save error: {e}")

    async def send_discord_notification(self, change_data: Dict):
        """Send Discord notification"""
        if not self.discord_webhook:
            return
        
        analysis = change_data['analysis']
        priority = self.get_priority_level(analysis['significance_score'])
        
        # Skip low priority unless very confident
        if priority == 'LOW' and analysis['confidence'] < 80:
            return
        
        # Create embed
        color_map = {'HIGH': 0xff0000, 'MEDIUM': 0xffa500, 'LOW': 0x00ff00}
        
        embed = {
            "title": f"🚨 EA FC Change Detected - {priority}",
            "color": color_map.get(priority, 0x0099ff),
            "timestamp": datetime.utcnow().isoformat(),
            "fields": [
                {"name": "Endpoint", "value": change_data['endpoint'], "inline": True},
                {"name": "Score", "value": str(analysis['significance_score']), "inline": True},
                {"name": "Type", "value": analysis['change_type'], "inline": True},
                {"name": "Confidence", "value": f"{analysis['confidence']}%", "inline": True}
            ]
        }
        
        # Add found content
        content_summary = []
        if analysis['found_sbcs']:
            content_summary.append(f"🏆 SBCs: {len(analysis['found_sbcs'])}")
        if analysis['found_promos']:
            content_summary.append(f"🎉 Promos: {len(analysis['found_promos'])}")
        if analysis['found_packs']:
            content_summary.append(f"📦 Packs: {len(analysis['found_packs'])}")
        
        if content_summary:
            embed["fields"].append({
                "name": "Content Found",
                "value": " | ".join(content_summary),
                "inline": False
            })
        
        payload = {"embeds": [embed]}
        
        try:
            async with self.session.post(self.discord_webhook, json=payload) as response:
                if response.status == 204:
                    logging.info("✅ Discord notification sent")
                else:
                    logging.error(f"❌ Discord failed: {response.status}")
        except Exception as e:
            logging.error(f"Discord error: {e}")

    def get_priority_level(self, score):
        """Get priority level from score"""
        if score >= self.high_threshold:
            return 'HIGH'
        elif score >= self.medium_threshold:
            return 'MEDIUM'
        elif score >= self.low_threshold:
            return 'LOW'
        return None

    async def monitoring_loop(self):
        """Main monitoring loop"""
        await self.initialize_session()
        
        logging.info(f"🔄 Starting monitoring loop - {len(self.endpoints)} endpoints")
        logging.info(f"⏰ Check interval: {self.check_interval//60} minutes")
        
        cycle_count = 0
        
        try:
            while self.running:
                cycle_count += 1
                logging.info(f"🔄 Cycle {cycle_count} starting...")
                
                changes_detected = []
                
                # Check all endpoints
                for name, url in self.endpoints.items():
                    if not self.running:  # Check if we should stop
                        break
                        
                    change_data = await self.check_endpoint(name, url)
                    if change_data:
                        changes_detected.append(change_data)
                
                if changes_detected:
                    high_sig = len([c for c in changes_detected if c['analysis']['significance_score'] > 10])
                    logging.warning(f"🎯 {len(changes_detected)} changes ({high_sig} high-sig)")
                else:
                    logging.info("✅ No changes detected")
                
                # Wait for next cycle
                if self.running:
                    await asyncio.sleep(self.check_interval)
                
        except Exception as e:
            logging.error(f"💥 Monitoring loop error: {e}")
        finally:
            if self.session:
                await self.session.close()

    async def health_check_handler(self, request):
        """Health check endpoint for Railway"""
        return web.json_response({
            'status': 'healthy',
            'uptime': str(datetime.now()),
            'endpoints_monitored': len(self.endpoints),
            'changes_detected': len(self.changes_log),
            'running': self.running
        })

    async def stats_handler(self, request):
        """Stats endpoint"""
        # Get recent changes from database
        try:
            with sqlite3.connect(self.db_path) as conn:
                recent_changes = conn.execute(
                    "SELECT COUNT(*) FROM changes WHERE timestamp > datetime('now', '-24 hours')"
                ).fetchone()[0]
                
                high_sig_changes = conn.execute(
                    "SELECT COUNT(*) FROM changes WHERE significance_score > ? AND timestamp > datetime('now', '-24 hours')",
                    (self.high_threshold,)
                ).fetchone()[0]
        except:
            recent_changes = 0
            high_sig_changes = 0
        
        return web.json_response({
            'recent_changes_24h': recent_changes,
            'high_significance_changes_24h': high_sig_changes,
            'endpoints_monitored': len(self.endpoints),
            'check_interval_minutes': self.check_interval // 60,
            'last_changes': self.changes_log[-5:] if self.changes_log else []
        })

    async def start_web_server(self):
        """Start web server for Railway health checks"""
        app = web.Application()
        app.router.add_get('/', self.health_check_handler)
        app.router.add_get('/health', self.health_check_handler)
        app.router.add_get('/stats', self.stats_handler)
        
        runner = web.AppRunner(app)
        await runner.setup()
        
        site = web.TCPSite(runner, '0.0.0.0', self.port)
        await site.start()
        
        logging.info(f"🌐 Web server started on port {self.port}")

    async def start(self):
        """Start the dataminer"""
        self.running = True
        
        # Handle shutdown gracefully
        def signal_handler(signum, frame):
            logging.info("🛑 Shutdown signal received")
            self.running = False
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # Start web server and monitoring concurrently
        await asyncio.gather(
            self.start_web_server(),
            self.monitoring_loop()
        )

# Main execution
async def main():
    print("""
╔══════════════════════════════════════════════════════════════╗
║              EA FC DataMiner - Railway Edition               ║
║                   Cloud-Optimized Version                   ║
╚══════════════════════════════════════════════════════════════╝
    """)
    
    dataminer = RailwayEAFCDataMiner()
    await dataminer.start()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("🏁 DataMiner stopped")
