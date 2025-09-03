#!/usr/bin/env python3
"""
EA FC DataMiner - Real-time Notification System
Sends alerts via Discord, Email, SMS, or desktop notifications
"""

import json
import smtplib
import requests
import asyncio
import aiohttp
from datetime import datetime
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging
from typing import Dict, List
import os

# Try to import optional dependencies
try:
    import win10toast  # Windows desktop notifications
    HAS_TOAST = True
except ImportError:
    HAS_TOAST = False

try:
    from twilio.rest import Client  # SMS notifications
    HAS_TWILIO = True
except ImportError:
    HAS_TWILIO = False

class NotificationManager:
    def __init__(self, config_file="config/notifications.json"):
        self.config_file = Path(config_file)
        self.config = self.load_config()
        
        # Initialize notification services
        self.discord_webhook = self.config.get('discord', {}).get('webhook_url')
        self.email_config = self.config.get('email', {})
        self.sms_config = self.config.get('sms', {})
        self.desktop_enabled = self.config.get('desktop', {}).get('enabled', True)
        
        # Notification thresholds
        self.thresholds = self.config.get('thresholds', {
            'high_priority': 15,
            'medium_priority': 8,
            'low_priority': 3
        })
        
        self.setup_logging()
    
    def load_config(self):
        """Load notification configuration"""
        self.config_file.parent.mkdir(exist_ok=True)
        
        if self.config_file.exists():
            with open(self.config_file, 'r') as f:
                return json.load(f)
        else:
            # Create default config
            default_config = {
                "discord": {
                    "webhook_url": "",
                    "enabled": False,
                    "mention_role": "",  # @everyone, @here, or role ID
                    "avatar_url": "https://cdn.discordapp.com/attachments/123/ea_fc_bot.png"
                },
                "email": {
                    "enabled": False,
                    "smtp_server": "smtp.gmail.com",
                    "smtp_port": 587,
                    "username": "your_email@gmail.com",
                    "password": "your_app_password",
                    "to_addresses": ["your_phone@gmail.com"]
                },
                "sms": {
                    "enabled": False,
                    "twilio_account_sid": "",
                    "twilio_auth_token": "",
                    "twilio_phone": "+1234567890",
                    "to_phones": ["+1234567890"]
                },
                "desktop": {
                    "enabled": True,
                    "duration": 10
                },
                "thresholds": {
                    "high_priority": 15,
                    "medium_priority": 8,
                    "low_priority": 3
                },
                "filters": {
                    "keywords": ["toty", "tots", "icon", "hero", "sbc"],
                    "endpoints": ["api_sbc", "api_objectives", "api_packs"],
                    "minimum_confidence": 75
                }
            }
            
            with open(self.config_file, 'w') as f:
                json.dump(default_config, f, indent=2)
            
            print(f"📝 Created default config: {self.config_file}")
            print("Please edit the configuration file with your notification settings")
            
            return default_config
    
    def setup_logging(self):
        """Setup logging for notifications"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - NOTIFY - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('notifications.log'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)
    
    def get_priority_level(self, significance_score):
        """Determine priority level based on significance score"""
        if significance_score >= self.thresholds['high_priority']:
            return 'HIGH'
        elif significance_score >= self.thresholds['medium_priority']:
            return 'MEDIUM'
        elif significance_score >= self.thresholds['low_priority']:
            return 'LOW'
        else:
            return None  # Don't notify
    
    def should_notify(self, change_data):
        """Determine if we should send notification based on filters"""
        analysis = change_data['analysis']
        
        # Check minimum confidence
        min_confidence = self.config.get('filters', {}).get('minimum_confidence', 50)
        if analysis.get('confidence', 0) < min_confidence:
            return False
        
        # Check if endpoint is in filter list
        endpoint_filters = self.config.get('filters', {}).get('endpoints', [])
        if endpoint_filters and change_data['endpoint'] not in endpoint_filters:
            return False
        
        # Check for keyword matches
        keyword_filters = self.config.get('filters', {}).get('keywords', [])
        if keyword_filters:
            content_text = str(analysis).lower()
            if not any(keyword.lower() in content_text for keyword in keyword_filters):
                return False
        
        # Check significance threshold
        priority = self.get_priority_level(analysis['significance_score'])
        return priority is not None
    
    def format_change_message(self, change_data, priority='MEDIUM'):
        """Format change data into notification message"""
        analysis = change_data['analysis']
        timestamp = datetime.fromisoformat(change_data['timestamp']).strftime('%H:%M:%S')
        
        # Priority emoji
        priority_emojis = {
            'HIGH': '🚨🚨🚨',
            'MEDIUM': '⚠️',
            'LOW': '📢'
        }
        
        emoji = priority_emojis.get(priority, '📢')
        
        # Build message
        title = f"{emoji} EA FC CHANGE DETECTED - {priority} PRIORITY"
        
        details = [
            f"**Time**: {timestamp}",
            f"**Endpoint**: {change_data['endpoint']}",
            f"**Significance Score**: {analysis['significance_score']}",
            f"**Change Type**: {analysis.get('change_type', 'unknown')}",
            f"**Confidence**: {analysis.get('confidence', 0)}%"
        ]
        
        # Add discovered content
        content_found = []
        if analysis.get('found_sbcs'):
            content_found.append(f"🏆 SBCs: {', '.join(analysis['found_sbcs'][:3])}")
        if analysis.get('found_promos'):
            content_found.append(f"🎉 Promos: {', '.join(analysis['found_promos'][:3])}")
        if analysis.get('found_packs'):
            content_found.append(f"📦 Packs: {', '.join(analysis['found_packs'][:3])}")
        if analysis.get('found_players'):
            content_found.append(f"⚽ Players: {', '.join(analysis['found_players'][:3])}")
        
        if content_found:
            details.extend(["", "**Content Found**:"] + content_found)
        
        message = f"{title}\n\n" + "\n".join(details)
        
        return {
            'title': title,
            'message': message,
            'short_message': f"EA FC: {analysis.get('change_type', 'Change')} detected (Score: {analysis['significance_score']})"
        }
    
    async def send_discord_notification(self, formatted_message, priority='MEDIUM'):
        """Send Discord webhook notification"""
        if not self.config['discord'].get('enabled') or not self.discord_webhook:
            return False
        
        # Color based on priority
        colors = {
            'HIGH': 0xff0000,    # Red
            'MEDIUM': 0xffa500,  # Orange  
            'LOW': 0x00ff00      # Green
        }
        
        # Mention role for high priority
        content = ""
        if priority == 'HIGH' and self.config['discord'].get('mention_role'):
            mention = self.config['discord']['mention_role']
            if mention == '@everyone':
                content = "@everyone"
            elif mention == '@here':
                content = "@here"
            else:
                content = f"<@&{mention}>"
        
        embed = {
            "title": formatted_message['title'],
            "description": formatted_message['message'],
            "color": colors.get(priority, 0x0099ff),
            "timestamp": datetime.utcnow().isoformat(),
            "footer": {"text": "EA FC DataMiner"},
            "thumbnail": {"url": self.config['discord'].get('avatar_url', '')}
        }
        
        payload = {
            "content": content,
            "embeds": [embed]
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.discord_webhook, json=payload) as response:
                    if response.status == 204:
                        self.logger.info("✅ Discord notification sent")
                        return True
                    else:
                        self.logger.error(f"❌ Discord notification failed: {response.status}")
                        return False
        except Exception as e:
            self.logger.error(f"❌ Discord notification error: {e}")
            return False
    
    def send_email_notification(self, formatted_message, priority='MEDIUM'):
        """Send email notification"""
        if not self.config['email'].get('enabled'):
            return False
        
        try:
            # Create message
            msg = MIMEMultipart()
            msg['From'] = self.config['email']['username']
            msg['Subject'] = formatted_message['title']
            
            # HTML body
            html_body = f"""
            <html>
            <body>
                <h2 style="color: {'red' if priority == 'HIGH' else 'orange' if priority == 'MEDIUM' else 'green'}">
                    {formatted_message['title']}
                </h2>
                <pre style="font-family: Arial, sans-serif; white-space: pre-wrap;">
{formatted_message['message']}
                </pre>
                <hr>
                <small>Generated by EA FC DataMiner at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</small>
            </body>
            </html>
            """
            
            msg.attach(MIMEText(html_body, 'html'))
            
            # Send to all addresses
            server = smtplib.SMTP(self.config['email']['smtp_server'], self.config['email']['smtp_port'])
            server.starttls()
            server.login(self.config['email']['username'], self.config['email']['password'])
            
            for to_addr in self.config['email']['to_addresses']:
                msg['To'] = to_addr
                server.send_message(msg)
                self.logger.info(f"✅ Email sent to {to_addr}")
            
            server.quit()
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Email notification error: {e}")
            return False
    
    def send_sms_notification(self, formatted_message, priority='MEDIUM'):
        """Send SMS notification via Twilio"""
        if not self.config['sms'].get('enabled') or not HAS_TWILIO:
            return False
        
        try:
            client = Client(
                self.config['sms']['twilio_account_sid'],
                self.config['sms']['twilio_auth_token']
            )
            
            # Use short message for SMS
            sms_text = formatted_message['short_message']
            
            for phone in self.config['sms']['to_phones']:
                message = client.messages.create(
                    body=sms_text,
                    from_=self.config['sms']['twilio_phone'],
                    to=phone
                )
                self.logger.info(f"✅ SMS sent to {phone}: {message.sid}")
            
            return True
            
        except Exception as e:
            self.logger.error(f"❌ SMS notification error: {e}")
            return False
    
    def send_desktop_notification(self, formatted_message, priority='MEDIUM'):
        """Send desktop notification"""
        if not self.config['desktop'].get('enabled'):
            return False
        
        try:
            if HAS_TOAST and os.name == 'nt':  # Windows
                toaster = win10toast.ToastNotifier()
                toaster.show_toast(
                    title="EA FC DataMiner",
                    msg=formatted_message['short_message'],
                    duration=self.config['desktop'].get('duration', 10),
                    icon_path=None
                )
                self.logger.info("✅ Desktop notification sent (Windows)")
                return True
            
            elif os.name == 'posix':  # Linux/Mac
                # Use notify-send on Linux
                import subprocess
                subprocess.run([
                    'notify-send',
                    'EA FC DataMiner',
                    formatted_message['short_message']
                ], check=False)
                self.logger.info("✅ Desktop notification sent (Linux)")
                return True
            
        except Exception as e:
            self.logger.error(f"❌ Desktop notification error: {e}")
            return False
    
    async def send_all_notifications(self, change_data):
        """Send notifications via all enabled channels"""
        if not self.should_notify(change_data):
            return False
        
        priority = self.get_priority_level(change_data['analysis']['significance_score'])
        formatted_message = self.format_change_message(change_data, priority)
        
        self.logger.info(f"🔔 Sending {priority} priority notifications")
        
        results = []
        
        # Discord (async)
        if self.config['discord'].get('enabled'):
            result = await self.send_discord_notification(formatted_message, priority)
            results.append(('Discord', result))
        
        # Email (sync)
        if self.config['email'].get('enabled'):
            result = self.send_email_notification(formatted_message, priority)
            results.append(('Email', result))
        
        # SMS (sync)
        if self.config['sms'].get('enabled'):
            result = self.send_sms_notification(formatted_message, priority)
            results.append(('SMS', result))
        
        # Desktop (sync)
        if self.config['desktop'].get('enabled'):
            result = self.send_desktop_notification(formatted_message, priority)
            results.append(('Desktop', result))
        
        # Log results
        successful = [channel for channel, success in results if success]
        failed = [channel for channel, success in results if not success]
        
        if successful:
            self.logger.info(f"✅ Notifications sent via: {', '.join(successful)}")
        if failed:
            self.logger.warning(f"❌ Failed notifications: {', '.join(failed)}")
        
        return len(successful) > 0
    
    def test_notifications(self):
        """Test all notification channels with sample data"""
        test_change_data = {
            'timestamp': datetime.now().isoformat(),
            'endpoint': 'test_endpoint',
            'analysis': {
                'significance_score': 20,
                'change_type': 'test_notification',
                'confidence': 95,
                'found_sbcs': ['Test SBC Challenge'],
                'found_promos': ['Test Promo Event'],
                'found_packs': ['Test Premium Pack'],
                'found_players': ['Test Player']
            }
        }
        
        print("🧪 Testing notification system...")
        
        # Test each service individually
        formatted_message = self.format_change_message(test_change_data, 'HIGH')
        
        if self.config['discord'].get('enabled'):
            print("Testing Discord...")
            asyncio.run(self.send_discord_notification(formatted_message, 'HIGH'))
        
        if self.config['email'].get('enabled'):
            print("Testing Email...")
            self.send_email_notification(formatted_message, 'HIGH')
        
        if self.config['sms'].get('enabled'):
            print("Testing SMS...")
            self.send_sms_notification(formatted_message, 'HIGH')
        
        if self.config['desktop'].get('enabled'):
            print("Testing Desktop...")
            self.send_desktop_notification(formatted_message, 'HIGH')
        
        print("🧪 Test notifications complete!")

# Integration with main dataminer
class NotificationIntegrator:
    """Integrates notifications with the main EA FC DataMiner"""
    
    def __init__(self, notification_manager=None):
        self.notification_manager = notification_manager or NotificationManager()
    
    async def handle_change_detected(self, change_data):
        """Handle change detection from main dataminer"""
        try:
            await self.notification_manager.send_all_notifications(change_data)
        except Exception as e:
            logging.error(f"Notification error: {e}")
    
    def integrate_with_dataminer(self, dataminer_instance):
        """Add notification hooks to existing dataminer"""
        original_process_change = dataminer_instance.process_change
        
        async def enhanced_process_change(name, url, content):
            # Call original method
            change_data = await original_process_change(name, url, content)
            
            # Send notifications
            if change_data:
                await self.handle_change_detected(change_data)
            
            return change_data
        
        # Replace method
        dataminer_instance.process_change = enhanced_process_change
        return dataminer_instance

# Configuration helper
def setup_notifications():
    """Interactive setup for notification configuration"""
    print("""
╔══════════════════════════════════════════════════════════════╗
║               EA FC Notification Setup                      ║
╚══════════════════════════════════════════════════════════════╝
    """)
    
    config_dir = Path("config")
    config_dir.mkdir(exist_ok=True)
    config_file = config_dir / "notifications.json"
    
    config = {}
    
    # Discord setup
    print("\n🎮 Discord Notifications Setup")
    discord_enabled = input("Enable Discord notifications? (y/n): ").lower() == 'y'
    if discord_enabled:
        webhook_url = input("Discord Webhook URL: ")
        mention_role = input("Role to mention for high priority (optional, @everyone/@here/role_id): ")
        
        config['discord'] = {
            'enabled': True,
            'webhook_url': webhook_url,
            'mention_role': mention_role,
            'avatar_url': 'https://cdn.discordapp.com/emojis/123456789/ea_fc.png'
        }
    else:
        config['discord'] = {'enabled': False}
    
    # Email setup
    print("\n📧 Email Notifications Setup")
    email_enabled = input("Enable Email notifications? (y/n): ").lower() == 'y'
    if email_enabled:
        smtp_server = input("SMTP Server (default: smtp.gmail.com): ") or "smtp.gmail.com"
        smtp_port = int(input("SMTP Port (default: 587): ") or 587)
        username = input("Email Username: ")
        password = input("Email Password (use app password for Gmail): ")
        to_addresses = input("To Email Addresses (comma separated): ").split(',')
        to_addresses = [addr.strip() for addr in to_addresses if addr.strip()]
        
        config['email'] = {
            'enabled': True,
            'smtp_server': smtp_server,
            'smtp_port': smtp_port,
            'username': username,
            'password': password,
            'to_addresses': to_addresses
        }
    else:
        config['email'] = {'enabled': False}
    
    # SMS setup
    print("\n📱 SMS Notifications Setup")
    sms_enabled = input("Enable SMS notifications via Twilio? (y/n): ").lower() == 'y'
    if sms_enabled:
        account_sid = input("Twilio Account SID: ")
        auth_token = input("Twilio Auth Token: ")
        twilio_phone = input("Twilio Phone Number (e.g., +1234567890): ")
        to_phones = input("To Phone Numbers (comma separated): ").split(',')
        to_phones = [phone.strip() for phone in to_phones if phone.strip()]
        
        config['sms'] = {
            'enabled': True,
            'twilio_account_sid': account_sid,
            'twilio_auth_token': auth_token,
            'twilio_phone': twilio_phone,
            'to_phones': to_phones
        }
    else:
        config['sms'] = {'enabled': False}
    
    # Desktop notifications
    print("\n🖥️ Desktop Notifications Setup")
    desktop_enabled = input("Enable Desktop notifications? (y/n): ").lower() == 'y'
    config['desktop'] = {
        'enabled': desktop_enabled,
        'duration': 10
    }
    
    # Thresholds
    print("\n⚖️ Notification Thresholds Setup")
    high_threshold = int(input("High Priority Threshold (default: 15): ") or 15)
    medium_threshold = int(input("Medium Priority Threshold (default: 8): ") or 8)
    low_threshold = int(input("Low Priority Threshold (default: 3): ") or 3)
    
    config['thresholds'] = {
        'high_priority': high_threshold,
        'medium_priority': medium_threshold,
        'low_priority': low_threshold
    }
    
    # Filters
    print("\n🎯 Content Filters Setup")
    keywords = input("Keywords to watch for (comma separated, e.g., toty,tots,icon): ")
    keywords = [kw.strip().lower() for kw in keywords.split(',') if kw.strip()]
    
    min_confidence = int(input("Minimum Confidence Score (0-100, default: 75): ") or 75)
    
    config['filters'] = {
        'keywords': keywords,
        'minimum_confidence': min_confidence,
        'endpoints': []  # Will be populated automatically
    }
    
    # Save configuration
    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)
    
    print(f"\n✅ Configuration saved to: {config_file}")
    
    # Test notifications
    test_now = input("\nTest notifications now? (y/n): ").lower() == 'y'
    if test_now:
        notification_manager = NotificationManager(config_file)
        notification_manager.test_notifications()
    
    return config_file

# Main CLI
def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="EA FC Notification System")
    parser.add_argument("--setup", action="store_true", help="Run interactive setup")
    parser.add_argument("--test", action="store_true", help="Test notifications")
    parser.add_argument("--config", default="config/notifications.json", help="Config file path")
    
    args = parser.parse_args()
    
    if args.setup:
        setup_notifications()
    elif args.test:
        notification_manager = NotificationManager(args.config)
        notification_manager.test_notifications()
    else:
        print("EA FC Notification System")
        print("Use --setup for initial configuration")
        print("Use --test to test notifications")

if __name__ == "__main__":
    main()
