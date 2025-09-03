#!/usr/bin/env python3
"""
EA FC DataMiner - Railway Deployment Version
Optimized for cloud deployment with health checks, environment variables, and web interface
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
        
        # ---------- Endpoints (includes all from both HARs) ----------
        # Some endpoints have per-request overrides (method/headers/json/expect)
        self.endpoints = {
            # ---------- Public web / assets ----------
            "web_remote_config": "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/content/25E4CDAE-799B-45BE-B257-667FDCDE8044/2025/fut/config/companion/remoteConfig.json",
            "web_app_main": "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/",
            "web_loc_en": {
                "url": "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/loc/messages_en.json",
                "headers": {
                    "Referer": "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/",
                    "Origin": "https://www.ea.com",
                    "Accept": "application/json"
                },
                "expect": [200, 403]
            },
            "web_loc_es": {
                "url": "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/loc/messages_es.json",
                "headers": {
                    "Referer": "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/",
                    "Origin": "https://www.ea.com",
                    "Accept": "application/json"
                },
                "expect": [200, 403]
            },
            "companion_config": {
                "url": "https://www.ea.com/ea-sports-fc/ultimate-team/companion-app/config/config.json",
                "headers": {
                    "Referer": "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/",
                    "Origin": "https://www.ea.com",
                    "Accept": "application/json"
                },
                "expect": [200, 403]
            },
            "web_js_main": {
                "url": "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/main.js",
                "headers": {
                    "Referer": "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/",
                    "Origin": "https://www.ea.com",
                    "Accept": "application/javascript, text/javascript, */*; q=0.1"
                },
                "expect": [200, 403]
            },
            "web_js_vendor": {
                "url": "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/vendor.js",
                "headers": {
                    "Referer": "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/",
                    "Origin": "https://www.ea.com",
                    "Accept": "application/javascript, text/javascript, */*; q=0.1"
                },
                "expect": [200, 403]
            },
            "web_css_main": {
                "url": "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/main.css",
                "headers": {
                    "Referer": "https://www.ea.com/ea-sports-fc/ultimate-team/web-app/",
                    "Origin": "https://www.ea.com",
                    "Accept": "text/css,*/*;q=0.1"
                },
                "expect": [200, 403]
            },
            "cdn_images_logo_black": {
                # Use a concrete file instead of a directory root to avoid 404 noise
                "url": "https://media.contentapi.ea.com/content/dam/eacom/ea-sports-fc/common/logos/ea-sports-fc-logo-black.svg",
                "expect": [200, 403, 404]
            },
            "static_assets_root": "https://static.ea.com/ea-sports-fc/ultimate-team/",

            # ---------- Telemetry ----------
            "pin_events": {
                "url": "https://pin-river.data.ea.com/pinEvents",
                "method": "POST",
                "json": {"events": []},
                "headers": {"Content-Type": "application/json"},
                "expect": [200, 204, 405]
            },

            # ---------- FCAS (Auth expected on many) ----------
            "fcas_auth": {
                "url": "https://fcas.mob.v4.prd.futc-ext.gcp.ea.com/fc/auth",
                "expect": [401, 404, 200]
            },
            "fcas_user_objectives_list": "https://fcas.mob.v4.prd.futc-ext.gcp.ea.com/fc/user/objective/list",
            "fcas_user_season": "https://fcas.mob.v4.prd.futc-ext.gcp.ea.com/fc/user/season",
            "fcas_user_season_lite": "https://fcas.mob.v4.prd.futc-ext.gcp.ea.com/fc/user/season/lite",

            # ---------- UTAS core (Auth expected) ----------
            # Club / squads / inventory
            "club_overview": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/club",
            "club_stats": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/club/stats/club",
            "club_consumables_dev": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/club/consumables/development",
            "stadium": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/stadium",
            "squad_active": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/squad/active",
            "squad_list": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/squad/list",

            # Store / purchased
            "store_purchase_groups_all": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/store/purchaseGroup/all?ppInfo=true&categoryInfo=true",
            "store_category_sku": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/sku/FFA25PS5/store/category",
            "purchased_items": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/purchased/items",

            # Featured squads / TOTW
            "featured_squad_history_totw": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/featuredsquad/fullhistory?featureConsumerId=sqbttotw",
            "featured_squad_id_124404": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/featuredsquad/124404?featureConsumerId=sqbttotw",

            # SBCs
            "sbc_sets": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/sbs/sets",
            "sbc_set_challenges_1244": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/sbs/setId/1244/challenges",
            "sbc_challenge_4333": {
                "url": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/sbs/challenge/4333",
                "expect": [200, 401, 403, 404]
            },
            "sbc_challenge_4333_squad": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/sbs/challenge/4333/squad",

            # Live messages / templates
            "livemsg_companion_store_tab": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/livemessage/template?screen=companionstorefeaturedtab",
            "livemsg_web_fut": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/livemessage/template?screen=futweblivemsg",
            "message_list_template": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/message/list/template?nucPersId=245151837&screen=webfuthub",

            # Objectives / SCMP
            "scmp_data_lite": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/scmp/data/lite",
            "scmp_objective_categories_all": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/scmp/objective/categories/all",

            # Competitive hubs
            "rivals_user_hub": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/rivals/v2/user/hub",
            "champs2_user_hub": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/champs2/user/hub",
            "sqbt_user_hub": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/sqbt/user/hub",

            # Social
            "social_hub": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/social/hub",

            # Leaderboards
            "leaderboards_options": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/leaderboards/options",
            "leaderboards_trader_friends_monthly": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/leaderboards/period/monthly/category/trader/view/friends?platform=local",

            # Watchlist / tradepile
            "watchlist": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/watchlist",
            "tradepile": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/tradepile",

            # Attributes / metadata
            "attributes_metadata_def_67116627": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/attributes/metadata?defIds=67116627",

            # Settings
            "game_settings": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/settings",

            # Academy
            "academy_hub_v2": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/academy/hub/v2?offset=0&count=20&sortOrder=asc&slotStatus=NOT_STARTED",

            # Meta rewards / item attributes (long list of ids)
            "meta_rewards_attributes": "https://utas.mob.v4.prd.futc-ext.gcp.ea.com/ut/game/fc25/metaRewards/items/attributes?itemIds=5005104,6114032,5005098,5005099,5005114,6840832,5005100,6830817,5005116,100941645,6830798,8120328,5005103,5005101,5005102,5005117,6820748,5005115,5005122,67376164,6869171,6313068,100905937,84162632,5005118,184762707,134458366,5004362,5005105,5004363,6844132,5005119,84145111,117672189,5005123,5005120,100840299,5005107,5005121,5005112,5005113,134410713",
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

    async def check_endpoint(self, name: str, url_or_cfg) -> Optional[Dict]:
        """Check endpoint for changes (supports per-endpoint overrides)"""
        try:
            await asyncio.sleep(2)  # Rate limiting

            # allow dict config: {"url":..., "method":..., "headers":..., "json":..., "expect":[...]}
            if isinstance(url_or_cfg, dict):
                url = url_or_cfg.get("url")
                method = url_or_cfg.get("method", "GET").upper()
                extra_headers = url_or_cfg.get("headers") or {}
                json_body = url_or_cfg.get("json")
                expect = set(url_or_cfg.get("expect", []))
            else:
                url = url_or_cfg
                method = "GET"
                extra_headers = {}
                json_body = None
                expect = set()

            req_kwargs = {"headers": extra_headers}
            if json_body is not None:
                req_kwargs["json"] = json_body

            async with self.session.request(method, url, **req_kwargs) as response:
                status_code = response.status
                
                # Track status code changes
                await self.track_status_change(name, status_code)
                
                # treat “expected” non-200 as info, not warnings
                if expect and status_code in expect and status_code != 200:
                    logging.info(f"ℹ️ HTTP {status_code} (expected) for {name}")
                    return None

                if status_code == 200:
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
                        
                elif status_code == 401:
                    # Auth required - this is expected for some endpoints
                    logging.debug(f"🔒 Auth required: {name}")
                elif status_code == 429:
                    logging.warning(f"⏰ Rate limited: {name}")
                    await asyncio.sleep(30)
                else:
                    logging.warning(f"⚠️ HTTP {status_code} for {name}")
                    
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
        
        # Save discovered content items
        await self.save_discovered_content(change_data)
        
        # Send notification if significant
        if analysis['significance_score'] > self.low_threshold:
            await self.send_discord_notification(change_data)
        
        # Keep in-memory log
        self.changes_log.append(change_data)
        if len(self.changes_log) > 100:  # Keep only last 100 changes
            self.changes_log.pop(0)
        
        return change_data

    async def save_discovered_content(self, change_data: Dict):
        """Save individual discovered content items"""
        analysis = change_data['analysis']
        timestamp = change_data['timestamp']
        endpoint = change_data['endpoint']
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Save SBCs
                for sbc in analysis.get('found_sbcs', []):
                    conn.execute(
                        "INSERT INTO discovered_content (timestamp, content_type, name, confidence_score, endpoint) VALUES (?, ?, ?, ?, ?)",
                        (timestamp, 'SBC', str(sbc), analysis['confidence'], endpoint)
                    )
                
                # Save Promos
                for promo in analysis.get('found_promos', []):
                    conn.execute(
                        "INSERT INTO discovered_content (timestamp, content_type, name, confidence_score, endpoint) VALUES (?, ?, ?, ?, ?)",
                        (timestamp, 'Promo', str(promo), analysis['confidence'], endpoint)
                    )
                
                # Save Packs
                for pack in analysis.get('found_packs', []):
                    conn.execute(
                        "INSERT INTO discovered_content (timestamp, content_type, name, confidence_score, endpoint) VALUES (?, ?, ?, ?, ?)",
                        (timestamp, 'Pack', str(pack), analysis['confidence'], endpoint)
                    )
                
                # Save Objectives
                for obj in analysis.get('found_objectives', []):
                    conn.execute(
                        "INSERT INTO discovered_content (timestamp, content_type, name, confidence_score, endpoint) VALUES (?, ?, ?, ?, ?)",
                        (timestamp, 'Objective', str(obj), analysis['confidence'], endpoint)
                    )
                
                # Save Features
                for feature in analysis.get('found_features', []):
                    conn.execute(
                        "INSERT INTO discovered_content (timestamp, content_type, name, confidence_score, endpoint) VALUES (?, ?, ?, ?, ?)",
                        (timestamp, 'Feature', str(feature), analysis['confidence'], endpoint)
                    )
                
        except Exception as e:
            logging.error(f"Error saving discovered content: {e}")

    async def analyze_content(self, content: str) -> Dict:
        """Analyze content for EA FC patterns with enhanced detection"""
        analysis = {
            'found_sbcs': [],
            'found_evolutions': [],
            'found_promos': [],
            'found_players': [],
            'found_packs': [],
            'found_objectives': [],
            'found_features': [],
            'found_competitive': [],
            'found_market': [],
            'found_social': [],
            'significance_score': 0,
            'change_type': 'unknown',
            'confidence': 50
        }
        
        # Enhanced pattern matching with new categories
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
                    
            elif category == 'evolution_indicators':
                analysis['found_evolutions'] = list(set(matches))[:10]
                analysis['significance_score'] += len(matches) * 8  # Evolutions are high value
                if matches:
                    analysis['change_type'] = 'evolution_update'
                    analysis['confidence'] = 88
                    
            elif category == 'promo_indicators':
                analysis['found_promos'] = list(set(matches))[:10]
                analysis['significance_score'] += len(matches) * 8
                if matches:
                    analysis['change_type'] = 'promo_update'
                    analysis['confidence'] = 90
                    
            elif category == 'competitive_indicators':
                analysis['found_competitive'] = list(set(matches))[:10]
                analysis['significance_score'] += len(matches) * 6  # FUT Champs/Rivals changes important
                if matches:
                    analysis['change_type'] = 'competitive_update'
                    analysis['confidence'] = 82
                    
            elif category == 'player_indicators':
                analysis['found_players'] = list(set(matches))[:10]
                analysis['significance_score'] += len(matches) * 3
                if matches:
                    analysis['change_type'] = 'player_update'
                    analysis['confidence'] = 65
                    
            elif category == 'pack_indicators':
                analysis['found_packs'] = list(set(matches))[:10]
                analysis['significance_score'] += len(matches) * 4
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
                    
            elif category == 'market_indicators':
                analysis['found_market'] = list(set(matches))[:10]
                analysis['significance_score'] += len(matches) * 3
                if matches:
                    analysis['change_type'] = 'market_update'
                    analysis['confidence'] = 70
                    
            elif category == 'social_indicators':
                analysis['found_social'] = list(set(matches))[:10]
                analysis['significance_score'] += len(matches) * 2
                if matches:
                    analysis['change_type'] = 'social_update'
                    analysis['confidence'] = 60
        
        # High-value terms boost - expanded list
        high_value_terms = [
            'toty', 'tots', 'icon', 'hero', 'flashback', 'fut champions', 
            'lightning round', 'beta', 'new feature', 'evolution', 'academy',
            'weekend league', 'division rivals', 'squad battles', 'rewards',
            'promo', 'special card', 'limited time', 'pack odds'
        ]
        
        content_lower = content.lower()
        value_boost = 0
        for term in high_value_terms:
            if term in content_lower:
                value_boost += 8
                analysis['confidence'] = min(95, analysis['confidence'] + 8)
        
        analysis['significance_score'] += value_boost
        
        # JavaScript/config file specific boosts
        if any(indicator in content_lower for indicator in ['api/', 'endpoint', 'url:', 'config', 'auth']):
            analysis['significance_score'] += 4
            if analysis['change_type'] == 'unknown':
                analysis['change_type'] = 'config_update'

        # Endpoint-specific boosts based on URL patterns
        endpoint_indicators = {
            'academy': 12,  # Evolution content very valuable
            'champs': 10,   # FUT Champions updates important  
            'sbs': 10,      # SBC content critical
            'rivals': 8,    # Division Rivals updates
            'featured': 8,  # TOTW and featured squads
            'store': 7,     # Pack and store updates
            'tradepile': 5, # Market activity
            'squad': 4,     # Squad management
            'social': 3     # Social features
        }
        
        for indicator, boost in endpoint_indicators.items():
            if indicator in content_lower:
                analysis['significance_score'] += boost
                analysis['confidence'] = min(95, analysis['confidence'] + 5)

        # Final safety: ensure we always return a dict with a concrete change_type
        if analysis['change_type'] == 'unknown':
            analysis['change_type'] = 'config_update'

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

    async def changes_handler(self, request):
        """Web interface to view detected changes"""
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Get recent changes
                changes = conn.execute("""
                    SELECT timestamp, endpoint, change_type, significance_score, extracted_data
                    FROM changes 
                    ORDER BY timestamp DESC 
                    LIMIT 50
                """).fetchall()
                
                # Get discovered content
                content = conn.execute("""
                    SELECT timestamp, content_type, name, confidence_score, endpoint
                    FROM discovered_content 
                    ORDER BY timestamp DESC 
                    LIMIT 100
                """).fetchall()
        except:
            changes = []
            content = []
        
        # Build HTML page
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>EA FC DataMiner - Detected Changes</title>
            <style>
                body {{ 
                    font-family: 'Segoe UI', Arial, sans-serif; 
                    margin: 0; 
                    padding: 20px;
                    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%); 
                    color: #fff; 
                    min-height: 100vh;
                }}
                .container {{ 
                    max-width: 1400px; 
                    margin: 0 auto; 
                }}
                .header {{ 
                    text-align: center; 
                    margin-bottom: 30px; 
                    background: rgba(255, 255, 255, 0.1);
                    padding: 30px;
                    border-radius: 15px;
                    backdrop-filter: blur(10px);
                }}
                .header h1 {{
                    margin: 0;
                    font-size: 2.5em;
                    background: linear-gradient(45deg, #00d4ff, #ff6b6b, #4ecdc4);
                    background-clip: text;
                    -webkit-background-clip: text;
                    -webkit-text-fill-color: transparent;
                }}
                .stats-grid {{
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                    gap: 15px;
                    margin-bottom: 30px;
                }}
                .stat-card {{
                    background: rgba(255, 255, 255, 0.1);
                    padding: 20px;
                    border-radius: 10px;
                    text-align: center;
                    backdrop-filter: blur(5px);
                }}
                .stat-number {{
                    font-size: 2em;
                    font-weight: bold;
                    color: #00d4ff;
                }}
                .section {{ 
                    background: rgba(255, 255, 255, 0.05); 
                    padding: 25px; 
                    border-radius: 15px; 
                    margin-bottom: 25px; 
                    backdrop-filter: blur(10px);
                    border: 1px solid rgba(255, 255, 255, 0.1);
                }}
                .section h2 {{
                    margin-top: 0;
                    color: #00d4ff;
                    border-bottom: 2px solid #00d4ff;
                    padding-bottom: 10px;
                }}
                .change-item {{ 
                    background: rgba(255, 255, 255, 0.08); 
                    padding: 20px; 
                    margin: 15px 0; 
                    border-radius: 10px; 
                    border-left: 4px solid #00ff00; 
                    transition: transform 0.2s, box-shadow 0.2s;
                }}
                .change-item:hover {{
                    transform: translateY(-2px);
                    box-shadow: 0 8px 25px rgba(0, 0, 0, 0.3);
                }}
                .high-priority {{ border-left-color: #ff6b6b; }}
                .medium-priority {{ border-left-color: #ffa500; }}
                .low-priority {{ border-left-color: #4ecdc4; }}
                .timestamp {{ 
                    color: #888; 
                    font-size: 12px; 
                    font-family: 'Courier New', monospace;
                }}
                .score {{ 
                    background: linear-gradient(45deg, #667eea 0%, #764ba2 100%);
                    padding: 5px 12px; 
                    border-radius: 20px; 
                    color: #fff; 
                    font-weight: bold;
                    display: inline-block;
                }}
                .content-list {{ 
                    display: grid; 
                    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); 
                    gap: 15px; 
                }}
                .content-item {{ 
                    background: rgba(255, 255, 255, 0.08); 
                    padding: 15px; 
                    border-radius: 10px; 
                    transition: transform 0.2s;
                }}
                .content-item:hover {{
                    transform: scale(1.02);
                }}
                .content-type-sbc {{ border-left: 4px solid #ff6b6b; }}
                .content-type-promo {{ border-left: 4px solid #ffa500; }}
                .content-type-pack {{ border-left: 4px solid #4ecdc4; }}
                .content-type-objective {{ border-left: 4px solid #a8e6cf; }}
                .content-type-feature {{ border-left: 4px solid #dda0dd; }}
                .refresh {{ 
                    background: linear-gradient(45deg, #667eea 0%, #764ba2 100%);
                    color: white; 
                    padding: 12px 25px; 
                    text-decoration: none; 
                    border-radius: 25px; 
                    transition: all 0.3s;
                    display: inline-block;
                    margin: 10px;
                }}
                .refresh:hover {{
                    transform: translateY(-2px);
                    box-shadow: 0 8px 20px rgba(0, 0, 0, 0.3);
                }}
                .no-data {{
                    text-align: center;
                    color: #888;
                    font-style: italic;
                    padding: 40px;
                }}
                .content-summary {{
                    display: flex;
                    flex-wrap: wrap;
                    gap: 10px;
                    margin-top: 10px;
                }}
                .content-tag {{
                    background: rgba(0, 212, 255, 0.2);
                    border: 1px solid rgba(0, 212, 255, 0.4);
                    padding: 4px 8px;
                    border-radius: 12px;
                    font-size: 12px;
                    color: #00d4ff;
                }}
            </style>
            <meta http-equiv="refresh" content="300"> <!-- Auto refresh every 5 minutes -->
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>🚀 EA FC DataMiner</h1>
                    <p>Real-time Content Detection System</p>
                    <p style="font-size: 14px; opacity: 0.8;">Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
                    <a href="/changes" class="refresh">🔄 Refresh</a>
                    <a href="/stats" class="refresh">📊 JSON Stats</a>
                    <a href="/" class="refresh">❤️ Health Check</a>
                </div>
                
                <div class="stats-grid">
                    <div class="stat-card">
                        <div class="stat-number">{len(changes)}</div>
                        <div>Total Changes</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-number">{len([c for c in changes if len(c) > 3 and c[3] >= 15])}</div>
                        <div>High Priority</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-number">{len(content)}</div>
                        <div>Content Items</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-number">{len(self.endpoints)}</div>
                        <div>Endpoints Monitored</div>
                    </div>
                </div>
                
                <div class="section">
                    <h2>📊 Recent Changes ({len(changes)})</h2>
                    {self._render_changes_html(changes)}
                </div>
                
                <div class="section">
                    <h2>🔍 Discovered Content ({len(content)})</h2>
                    <div class="content-list">
                        {self._render_content_html(content)}
                    </div>
                </div>
            </div>
        </body>
        </html>
        """
        
        return web.Response(text=html, content_type='text/html')

    def _render_changes_html(self, changes):
        """Render changes as HTML"""
        if not changes:
            return '<div class="no-data">🔍 No changes detected yet.<br><br>The system is monitoring EA FC endpoints and will detect changes when EA updates their content. This usually happens when new SBCs, promos, or packs are released.</div>'
        
        html_parts = []
        for change in changes:
            timestamp, endpoint, change_type, score, extracted_data = change
            
            # Parse extracted data
            try:
                analysis = json.loads(extracted_data) if extracted_data else {}
            except:
                analysis = {}
            
            # Determine priority class
            priority_class = "low-priority"
            priority_text = "LOW"
            if score >= 15:
                priority_class = "high-priority"
                priority_text = "HIGH"
            elif score >= 8:
                priority_class = "medium-priority" 
                priority_text = "MEDIUM"
            
            # Build content summary tags
            content_tags = []
            if analysis.get('found_sbcs'):
                content_tags.append(f'🏆 SBCs ({len(analysis["found_sbcs"])})')
            if analysis.get('found_promos'):
                content_tags.append(f'🎉 Promos ({len(analysis["found_promos"])})')
            if analysis.get('found_packs'):
                content_tags.append(f'📦 Packs ({len(analysis["found_packs"])})')
            if analysis.get('found_objectives'):
                content_tags.append(f'🎯 Objectives ({len(analysis["found_objectives"])})')
            if analysis.get('found_features'):
                content_tags.append(f'🔧 Features ({len(analysis["found_features"])})')
            
            content_tags_html = ""
            if content_tags:
                content_tags_html = f'''
                <div class="content-summary">
                    {''.join([f'<span class="content-tag">{tag}</span>' for tag in content_tags])}
                </div>
                '''
            
            # Sample content preview for high priority changes
            sample_preview = ""
            if score >= 15 and (analysis.get('found_sbcs') or analysis.get('found_promos')):
                samples = []
                if analysis.get('found_sbcs'):
                    samples.extend([f"🏆 {sbc}" for sbc in analysis['found_sbcs'][:3]])
                if analysis.get('found_promos'):
                    samples.extend([f"🎉 {promo}" for promo in analysis['found_promos'][:3]])
                
                if samples:
                    sample_preview = f'<div style="margin-top: 10px; padding: 10px; background: rgba(255,255,255,0.05); border-radius: 5px; font-size: 13px;">{"<br>".join(samples[:5])}</div>'
            
            html_parts.append(f"""
            <div class="change-item {priority_class}">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                    <div>
                        <strong style="color: #00d4ff;">{endpoint}</strong>
                        <span style="margin-left: 15px; color: #888;">Priority: {priority_text}</span>
                    </div>
                    <span class="score">Score: {score}</span>
                </div>
                <div class="timestamp">{timestamp} | Type: {change_type} | Confidence: {analysis.get('confidence', 0)}%</div>
                {content_tags_html}
                {sample_preview}
            </div>
            """)
        
        return "".join(html_parts)

    def _render_content_html(self, content):
        """Render discovered content as HTML"""
        if not content:
            return '<div class="no-data">🔍 No specific content items discovered yet.<br><br>Content items like SBC names, promo titles, and pack names will appear here when detected in EA FC configuration changes.</div>'
        
        html_parts = []
        for item in content:
            timestamp, content_type, name, confidence, endpoint = item
            
            # Icon based on content type
            icons = {
                'SBC': '🏆',
                'Promo': '🎉', 
                'Pack': '📦',
                'Objective': '🎯',
                'Feature': '🔧',
                'Player': '⚽'
            }
            icon = icons.get(content_type, '📄')
            
            # CSS class for content type
            type_class = f"content-type-{content_type.lower()}"
            
            html_parts.append(f"""
            <div class="content-item {type_class}">
                <div style="display: flex; align-items: center; margin-bottom: 8px;">
                    <span style="font-size: 1.2em; margin-right: 8px;">{icon}</span>
                    <strong style="color: #fff;">{name}</strong>
                </div>
                <div class="timestamp">{timestamp}</div>
                <div style="margin-top: 5px;">
                    <span style="background: rgba(0,212,255,0.2); padding: 2px 6px; border-radius: 10px; font-size: 11px; color: #00d4ff;">
                        {content_type}
                    </span>
                    <span style="background: rgba(255,107,107,0.2); padding: 2px 6px; border-radius: 10px; font-size: 11px; color: #ff6b6b; margin-left: 5px;">
                        {confidence}% confidence
                    </span>
                </div>
                <div style="font-size: 12px; color: #888; margin-top: 8px;">Source: {endpoint}</div>
            </div>
            """)
        
        return "".join(html_parts)

    async def start_web_server(self):
        """Start web server for Railway health checks"""
        app = web.Application()
        app.router.add_get('/', self.health_check_handler)
        app.router.add_get('/health', self.health_check_handler)
        app.router.add_get('/stats', self.stats_handler)
        app.router.add_get('/changes', self.changes_handler)  # Web interface
        
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
║                      v2.1 - Web Interface                   ║
╚══════════════════════════════════════════════════════════════╝
    """)
    
    dataminer = RailwayEAFCDataMiner()
    await dataminer.start()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("🏁 DataMiner stopped")
