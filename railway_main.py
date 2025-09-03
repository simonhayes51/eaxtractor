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
        
        # EA FC endpoints - mix of public and monitored auth endpoints
        self.endpoints = {
            # ✅ Public endpoints (no auth required) - HIGH PRIORITY
            "remote_config": "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/content/25E4CDAE-799B-45BE-B257-667FDCDE8044/2025/fut/config/companion/remoteConfig.json",
            "web_app_main": "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/",
            "localization_en": "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/loc/messages_en.json",
            "localization_es": "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/loc/messages_es.json",
            "companion_config": "https://www.ea.com/ea-sports-fc/ultimate-team/companion-app/config/config.json",
            
            # 📱 Web app assets (contain hardcoded endpoints and features)
            "web_app_js_main": "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/main.js",
            "web_app_js_vendor": "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/vendor.js",
            "web_app_css": "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/main.css",
            
            # 🔧 Additional config files
            "version_config": "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/config/version.json",
            "feature_config": "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/config/features.json",
            
            # 🔍 Auth endpoint monitoring (401 expected, watching for changes)
            "sbc_sets_monitor": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/sbs/sets",
            "objectives_monitor": "https://fcas.mob.v4.prd.futc-ext.gcp.ea.com/fc/user/objective/list",
            "store_monitor": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/store/purchaseGroup/all",
            "totw_monitor": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/featuredsquad/fullhistory?featureConsumerId=sqbttotw",
            
            # 🌐 CDN and media endpoints
            "cdn_images": "https://media.contentapi.ea.com/content/dam/eacom/ea-sports-fc/",
            "static_assets": "https://static.ea.com/ea-sports-fc/ultimate-team/"
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
                r'sbc.*challenge',
                r'player.*exchange',
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
                r'special.*card',
                r'limited.*time',
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
                r'player.*update',
                r'new.*player',
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
                r'flash.*sale',
                r'special.*offer',
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
                r'milestone.*reward',
            ],
            'feature_indicators': [
                r'"featureName":\s*"([^"]+)"',
                r'"enabled":\s*(true|false)',
                r'"version":\s*"([^"]+)"',
                r'"releaseDate":\s*"([^"]+)"',
                r'new.*feature',
                r'beta.*test',
                r'experimental',
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
                    status_code INTEGER,
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
            
            conn.execute('''
                CREATE TABLE IF NOT EXISTS endpoint_status (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    endpoint TEXT NOT NULL,
                    status_code INTEGER,
                    last_checked TEXT,
                    status_changed TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')

    async def initialize_session(self):
        """Initialize aiohttp session"""
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/html, application/xhtml+xml, application/xml;q=0.9, image/webp, */*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'DNT': '1',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache'
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
                status_code = response.status
                
                # Track status code changes
                await self.track_status_change(name, status_code)
                
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
                        change_data = await self.process_change(name, url, content, status_code)
                        self.known_hashes[name] = current_hash
                        return change_data
                        
                elif response.status == 401:
                    # Auth required - this is expected for some endpoints
                    logging.debug(f"🔒 Auth required: {name}")
                elif response.status == 429:
                    logging.warning(f"⏰ Rate limited: {name}")
                    await asyncio.sleep(30)
                else:
                    logging.warning(f"⚠️ HTTP {response.status} for {name}")
                    
        except Exception as e:
            logging.error(f"💥 Error checking {name}: {e}")
        
        return None

    async def track_status_change(self, endpoint: str, status_code: int):
        """Track status code changes for endpoints"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Get last known status
                last_status = conn.execute(
                    "SELECT status_code FROM endpoint_status WHERE endpoint = ? ORDER BY created_at DESC LIMIT 1",
                    (endpoint,)
                ).fetchone()
                
                current_time = datetime.now().isoformat()
                
                if last_status is None:
                    # First time seeing this endpoint
                    conn.execute(
                        "INSERT INTO endpoint_status (endpoint, status_code, last_checked) VALUES (?, ?, ?)",
                        (endpoint, status_code, current_time)
                    )
                elif last_status[0] != status_code:
                    # Status changed!
                    conn.execute(
                        "INSERT INTO endpoint_status (endpoint, status_code, last_checked, status_changed) VALUES (?, ?, ?, ?)",
                        (endpoint, status_code, current_time, current_time)
                    )
                    
                    # Log significant status changes
                    if (last_status[0] == 401 and status_code == 200) or (last_status[0] == 404 and status_code == 200):
                        logging.warning(f"🎉 STATUS CHANGE: {endpoint} changed from {last_status[0]} to {status_code}")
                        
                        # Send high-priority notification for auth->success changes
                        if status_code == 200:
                            await self.send_status_change_notification(endpoint, last_status[0], status_code)
                
        except Exception as e:
            logging.error(f"Error tracking status for {endpoint}: {e}")

    async def send_status_change_notification(self, endpoint: str, old_status: int, new_status: int):
        """Send notification for significant status changes"""
        if not self.discord_webhook:
            return
        
        embed = {
            "title": "🎉 EA FC Endpoint Status Change!",
            "color": 0x00ff00,  # Green
            "timestamp": datetime.utcnow().isoformat(),
            "fields": [
                {"name": "Endpoint", "value": endpoint, "inline": False},
                {"name": "Status Change", "value": f"{old_status} → {new_status}", "inline": True},
                {"name": "Significance", "value": "HIGH - Possible auth bypass!", "inline": True}
            ],
            "description": f"Endpoint that was previously blocked is now accessible!"
        }
        
        payload = {"embeds": [embed]}
        
        try:
            async with self.session.post(self.discord_webhook, json=payload) as response:
                if response.status == 204:
                    logging.info("✅ Status change notification sent")
        except Exception as e:
            logging.error(f"Status notification error: {e}")

    async def process_change(self, name: str, url: str, content: str, status_code: int) -> Dict:
        """Process detected change"""
        timestamp = datetime.now()
        
        # Analyze content
        analysis = await self.analyze_content(content)
        
        change_data = {
            'timestamp': timestamp.isoformat(),
            'endpoint': name,
            'url': url,
            'analysis': analysis,
            'content_length': len(content),
            'status_code': status_code
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
            'found_objectives': [],
            'found_features': [],
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
                analysis['found_sbcs'] = list(set(matches))[:10]  # Limit to 10
                analysis['significance_score'] += len(matches) * 5
                if matches:
                    analysis['change_type'] = 'sbc_update'
                    analysis['confidence'] = 85
                    
            elif category == 'promo_indicators':
                analysis['found_promos'] = list(set(matches))[:10]
                analysis['significance_score'] += len(matches) * 8
                if matches:
                    analysis['change_type'] = 'promo_update'
                    analysis['confidence'] = 90
                    
            elif category == 'player_indicators':
                analysis['found_players'] = list(set(matches))[:10]
                analysis['significance_score'] += len(matches) * 2
                if matches:
                    analysis['change_type'] = 'player_update'
                    analysis['confidence'] = 60
                    
            elif category == 'pack_indicators':
                analysis['found_packs'] = list(set(matches))[:10]
                analysis['significance_score'] += len(matches) * 3
                if matches:
                    analysis['change_type'] = 'pack_update'
                    analysis['confidence'] = 75
                    
            elif category == 'objective_indicators':
                analysis['found_objectives'] = list(set(matches))[:10]
                analysis['significance_score'] += len(matches) * 4
                if matches:
                    analysis['change_type'] = 'objective_update'
                    analysis['confidence'] = 80
                    
            elif category == 'feature_indicators':
                analysis['found_features'] = list(set(matches))[:10]
                analysis['significance_score'] += len(matches) * 6
                if matches:
                    analysis['change_type'] = 'feature_update'
                    analysis['confidence'] = 85
        
        # High-value terms boost
        high_value_terms = ['toty', 'tots', 'icon', 'hero', 'flashback', 'fut champions', 'lightning round', 'beta', 'new feature']
        content_lower = content.lower()
        for term in high_value_terms:
            if term in content_lower:
                analysis['significance_score'] += 10
                analysis['confidence'] = min(95, analysis['confidence'] + 10)
        
        # JavaScript/config file specific boosts
        if any(indicator in content_lower for indicator in ['api/', 'endpoint', 'url:', 'config']):
            analysis['significance_score'] += 5
            analysis['change_type'] = 'config_update' if analysis['change_type'] == 'unknown' else analysis['change_type']
        
        return analysis

    async def save_to_database(self, change_data: Dict):
        """Save change to database"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('''
                    INSERT INTO changes 
                    (timestamp, endpoint, change_type, significance_score, content_hash, extracted_data, status_code)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    change_data['timestamp'],
                    change_data['endpoint'],
                    change_data['analysis']['change_type'],
                    change_data['analysis']['significance_score'],
                    self.get_file_hash(str(change_data['analysis'])),
                    json.dumps(change_data['analysis']),
                    change_data['status_code']
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
        priority_emojis = {'HIGH': '🚨🚨🚨', 'MEDIUM': '⚠️', 'LOW': '📢'}
        
        embed = {
            "title": f"{priority_emojis.get(priority, '📢')} EA FC Change Detected - {priority}",
            "color": color_map.get(priority, 0x0099ff),
            "timestamp": datetime.utcnow().isoformat(),
            "fields": [
                {"name": "Endpoint", "value": change_data['endpoint'], "inline": True},
                {"name": "Score", "value": str(analysis['significance_score']), "inline": True},
                {"name": "Type", "value": analysis['change_type'], "inline": True},
                {"name": "Confidence", "value": f"{analysis['confidence']}%", "inline": True}
            ]
        }
        
        # Add found content summary
        content_summary = []
        if analysis['found_sbcs']:
            content_summary.append(f"🏆 SBCs: {len(analysis['found_sbcs'])}")
        if analysis['found_promos']:
            content_summary.append(f"🎉 Promos: {len(analysis['found_promos'])}")
        if analysis['found_packs']:
            content_summary.append(f"📦 Packs: {len(analysis['found_packs'])}")
        if analysis['found_objectives']:
            content_summary.append(f"🎯 Objectives: {len(analysis['found_objectives'])}")
        if analysis['found_features']:
            content_summary.append(f"🔧 Features: {len(analysis['found_features'])}")
        
        if content_summary:
            embed["fields"].append({
                "name": "Content Found",
                "value": " | ".join(content_summary),
                "inline": False
            })
        
        # Add sample findings for high priority
        if priority == 'HIGH' and (analysis['found_sbcs'] or analysis['found_promos']):
            sample_content = []
            if analysis['found_sbcs']:
                sample_content.extend([f"SBC: {item}" for item in analysis['found_sbcs'][:3]])
            if analysis['found_promos']:
                sample_content.extend([f"Promo: {item}" for item in analysis['found_promos'][:3]])
            
            if sample_content:
                embed["fields"].append({
                    "name": "Sample Findings",
                    "value": "\n".join(sample_content),
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
            'running': self.running,
            'check_interval_minutes': self.check_interval // 60
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
                
                # Get endpoint status summary
                endpoint_stats = conn.execute("""
                    SELECT endpoint, status_code, COUNT(*) as checks
                    FROM endpoint_status 
                    WHERE last_checked > datetime('now', '-1 hours')
                    GROUP BY endpoint, status_code
                    ORDER BY endpoint
                """).fetchall()
        except:
            recent_changes = 0
            high_sig_changes = 0
            endpoint_stats = []
        
        return web.json_response({
            'recent_changes_24h': recent_changes,
            'high_significance_changes_24h': high_sig_changes,
            'endpoints_monitored': len(self.endpoints),
            'check_interval_minutes': self.check_interval // 60,
            'endpoint_status_summary': [{"endpoint": row[0], "status": row[1], "checks": row[2]} for row in endpoint_stats],
            'last_changes': self.changes_log[-5:] if self.changes_log else [],
            'thresholds': {
                'high': self.high_threshold,
                'medium': self.medium_threshold,
                'low': self.low_threshold
            }
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
║                      v2.0 - Enhanced                        ║
╚══════════════════════════════════════════════════════════════╝
    """)
    
    dataminer = RailwayEAFCDataMiner()
    await dataminer.start()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("🏁 DataMiner stopped")
