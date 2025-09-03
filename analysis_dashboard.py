#!/usr/bin/env python3
"""
EA FC DataMiner Analysis Dashboard
View and analyze your collected data with charts and statistics
"""

import sqlite3
import json
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from datetime import datetime, timedelta
from pathlib import Path
import argparse
from collections import Counter, defaultdict
import re

class EAFCAnalyzer:
    def __init__(self, db_path="data/ea_fc_changes.db"):
        self.db_path = Path(db_path)
        self.results_dir = Path("analysis_results")
        self.results_dir.mkdir(exist_ok=True)
        
        # Set up plotting style
        plt.style.use('seaborn-v0_8')
        sns.set_palette("husl")
        
    def get_connection(self):
        """Get database connection"""
        return sqlite3.connect(self.db_path)
    
    def generate_summary_stats(self, days=30):
        """Generate summary statistics for the last N days"""
        with self.get_connection() as conn:
            # Get data from last N days
            cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()
            
            stats = {}
            
            # Total changes
            stats['total_changes'] = conn.execute(
                "SELECT COUNT(*) FROM changes WHERE timestamp > ?", 
                (cutoff_date,)
            ).fetchone()[0]
            
            # High significance changes
            stats['high_significance'] = conn.execute(
                "SELECT COUNT(*) FROM changes WHERE timestamp > ? AND significance_score > 10",
                (cutoff_date,)
            ).fetchone()[0]
            
            # Changes by endpoint
            endpoint_data = conn.execute("""
                SELECT endpoint, COUNT(*) as count, AVG(significance_score) as avg_score
                FROM changes 
                WHERE timestamp > ?
                GROUP BY endpoint 
                ORDER BY count DESC
            """, (cutoff_date,)).fetchall()
            
            stats['top_endpoints'] = endpoint_data
            
            # Changes by type
            type_data = conn.execute("""
                SELECT change_type, COUNT(*) as count
                FROM changes 
                WHERE timestamp > ? AND change_type != 'unknown'
                GROUP BY change_type 
                ORDER BY count DESC
            """, (cutoff_date,)).fetchall()
            
            stats['change_types'] = type_data
            
            # Discovered content
            content_data = conn.execute("""
                SELECT content_type, COUNT(DISTINCT name) as unique_items
                FROM discovered_content 
                WHERE timestamp > ?
                GROUP BY content_type
                ORDER BY unique_items DESC
            """, (cutoff_date,)).fetchall()
            
            stats['content_discovered'] = content_data
            
            # Daily activity
            daily_data = conn.execute("""
                SELECT DATE(timestamp) as date, COUNT(*) as changes, 
                       AVG(significance_score) as avg_significance
                FROM changes 
                WHERE timestamp > ?
                GROUP BY DATE(timestamp)
                ORDER BY date DESC
            """, (cutoff_date,)).fetchall()
            
            stats['daily_activity'] = daily_data
            
        return stats
    
    def plot_activity_timeline(self, days=30):
        """Create timeline plot of activity"""
        stats = self.generate_summary_stats(days)
        
        if not stats['daily_activity']:
            print("No data available for timeline plot")
            return
        
        # Prepare data
        dates = [datetime.strptime(row[0], '%Y-%m-%d') for row in stats['daily_activity']]
        changes = [row[1] for row in stats['daily_activity']]
        significance = [row[2] for row in stats['daily_activity']]
        
        # Create subplot
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 10))
        
        # Changes over time
        ax1.plot(dates, changes, marker='o', linewidth=2, markersize=6)
        ax1.set_title(f'EA FC Changes Detected - Last {days} Days', fontsize=16, fontweight='bold')
        ax1.set_ylabel('Number of Changes', fontsize=12)
        ax1.grid(True, alpha=0.3)
        
        # Significance over time
        ax2.plot(dates, significance, marker='s', color='orange', linewidth=2, markersize=6)
        ax2.set_title('Average Significance Score Over Time', fontsize=14)
        ax2.set_ylabel('Avg Significance Score', fontsize=12)
        ax2.set_xlabel('Date', fontsize=12)
        ax2.grid(True, alpha=0.3)
        
        plt.tight_layout()
        plt.savefig(self.results_dir / f'activity_timeline_{days}d.png', dpi=300, bbox_inches='tight')
        plt.show()
    
    def plot_endpoint_analysis(self, days=30):
        """Analyze which endpoints are most active"""
        stats = self.generate_summary_stats(days)
        
        if not stats['top_endpoints']:
            print("No endpoint data available")
            return
        
        # Prepare data
        endpoints = [row[0] for row in stats['top_endpoints'][:10]]
        counts = [row[1] for row in stats['top_endpoints'][:10]]
        avg_scores = [row[2] for row in stats['top_endpoints'][:10]]
        
        # Create subplots
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 6))
        
        # Change frequency
        bars1 = ax1.barh(endpoints, counts, color='skyblue', edgecolor='navy', alpha=0.7)
        ax1.set_title('Most Active Endpoints', fontsize=14, fontweight='bold')
        ax1.set_xlabel('Number of Changes', fontsize=12)
        
        # Add value labels
        for bar, count in zip(bars1, counts):
            ax1.text(bar.get_width() + 0.1, bar.get_y() + bar.get_height()/2, 
                    str(count), va='center', fontweight='bold')
        
        # Average significance
        bars2 = ax2.barh(endpoints, avg_scores, color='lightcoral', edgecolor='darkred', alpha=0.7)
        ax2.set_title('Average Significance by Endpoint', fontsize=14, fontweight='bold')
        ax2.set_xlabel('Average Significance Score', fontsize=12)
        
        # Add value labels
        for bar, score in zip(bars2, avg_scores):
            ax2.text(bar.get_width() + 0.1, bar.get_y() + bar.get_height()/2, 
                    f'{score:.1f}', va='center', fontweight='bold')
        
        plt.tight_layout()
        plt.savefig(self.results_dir / f'endpoint_analysis_{days}d.png', dpi=300, bbox_inches='tight')
        plt.show()
    
    def plot_content_discovery(self, days=30):
        """Show discovered content breakdown"""
        stats = self.generate_summary_stats(days)
        
        if not stats['content_discovered']:
            print("No content discovery data available")
            return
        
        # Pie chart of content types
        content_types = [row[0] for row in stats['content_discovered']]
        counts = [row[1] for row in stats['content_discovered']]
        
        plt.figure(figsize=(10, 8))
        colors = plt.cm.Set3(range(len(content_types)))
        
        wedges, texts, autotexts = plt.pie(counts, labels=content_types, autopct='%1.1f%%',
                                          colors=colors, explode=[0.05]*len(content_types),
                                          shadow=True, startangle=90)
        
        plt.title(f'Discovered Content Types - Last {days} Days', fontsize=16, fontweight='bold')
        
        # Make text more readable
        for autotext in autotexts:
            autotext.set_color('white')
            autotext.set_fontweight('bold')
        
        plt.savefig(self.results_dir / f'content_discovery_{days}d.png', dpi=300, bbox_inches='tight')
        plt.show()
    
    def analyze_content_patterns(self, days=30):
        """Analyze patterns in discovered content"""
        with self.get_connection() as conn:
            cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()
            
            # Get all discovered content
            content_data = conn.execute("""
                SELECT name, content_type, confidence_score, timestamp
                FROM discovered_content 
                WHERE timestamp > ?
                ORDER BY confidence_score DESC, timestamp DESC
            """, (cutoff_date,)).fetchall()
        
        if not content_data:
            print("No content data to analyze")
            return
        
        print(f"\n🔍 Content Pattern Analysis - Last {days} Days")
        print("=" * 60)
        
        # Group by content type
        by_type = defaultdict(list)
        for name, content_type, confidence, timestamp in content_data:
            by_type[content_type].append({
                'name': name,
                'confidence': confidence,
                'timestamp': timestamp
            })
        
        for content_type, items in by_type.items():
            print(f"\n📋 {content_type} Items Found ({len(items)}):")
            print("-" * 40)
            
            # Show top items by confidence
            sorted_items = sorted(items, key=lambda x: x['confidence'], reverse=True)
            for item in sorted_items[:10]:  # Top 10
                print(f"  • {item['name']} (Confidence: {item['confidence']}%)")
                print(f"    Detected: {item['timestamp'][:19]}")
        
        # Look for naming patterns
        print(f"\n🎯 Pattern Analysis:")
        print("-" * 40)
        
        all_names = [item[0] for item in content_data]
        
        # Common words in names
        word_counter = Counter()
        for name in all_names:
            words = re.findall(r'\b\w+\b', name.lower())
            word_counter.update(words)
        
        print("Most common words in content names:")
        for word, count in word_counter.most_common(10):
            if len(word) > 3:  # Skip short words
                print(f"  • '{word}': {count} times")
    
    def export_weekly_report(self):
        """Generate comprehensive weekly report"""
        stats = self.generate_summary_stats(7)  # Last 7 days
        
        report_path = self.results_dir / f"weekly_analysis_{datetime.now().strftime('%Y%m%d')}.md"
        
        with open(report_path, 'w') as f:
            f.write(f"# EA FC Weekly Analysis Report\n")
            f.write(f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            
            f.write(f"## 📊 Summary Statistics (Last 7 Days)\n")
            f.write(f"- **Total Changes Detected**: {stats['total_changes']}\n")
            f.write(f"- **High Significance Changes**: {stats['high_significance']}\n")
            f.write(f"- **Success Rate**: {(stats['high_significance']/max(stats['total_changes'],1)*100):.1f}% high-value detection\n\n")
            
            if stats['top_endpoints']:
                f.write(f"## 🎯 Most Active Endpoints\n")
                for endpoint, count, avg_score in stats['top_endpoints'][:5]:
                    f.write(f"- **{endpoint}**: {count} changes (avg score: {avg_score:.1f})\n")
                f.write("\n")
            
            if stats['change_types']:
                f.write(f"## 📋 Content Types Detected\n")
                for change_type, count in stats['change_types']:
                    f.write(f"- **{change_type}**: {count} instances\n")
                f.write("\n")
            
            if stats['content_discovered']:
                f.write(f"## 🔍 Content Discovery Summary\n")
                for content_type, count in stats['content_discovered']:
                    f.write(f"- **{content_type}**: {count} unique items\n")
                f.write("\n")
            
            f.write(f"## 📈 Trend Analysis\n")
            if stats['daily_activity']:
                avg_daily = sum(row[1] for row in stats['daily_activity']) / len(stats['daily_activity'])
                f.write(f"- **Average Daily Changes**: {avg_daily:.1f}\n")
                
                most_active_day = max(stats['daily_activity'], key=lambda x: x[1])
                f.write(f"- **Most Active Day**: {most_active_day[0]} ({most_active_day[1]} changes)\n")
            
        print(f"📄 Weekly report saved to: {report_path}")
        return report_path
    
    def interactive_analysis(self):
        """Interactive analysis menu"""
        while True:
            print("\n" + "="*60)
            print("🔍 EA FC DataMiner - Interactive Analysis")
            print("="*60)
            print("1. Activity Timeline (30 days)")
            print("2. Endpoint Analysis") 
            print("3. Content Discovery Chart")
            print("4. Content Pattern Analysis")
            print("5. Generate Weekly Report")
            print("6. Custom Date Range Analysis")
            print("7. Database Statistics")
            print("0. Exit")
            
            choice = input("\nSelect option (0-7): ").strip()
            
            if choice == '0':
                break
            elif choice == '1':
                days = int(input("Number of days (default 30): ") or 30)
                self.plot_activity_timeline(days)
            elif choice == '2':
                days = int(input("Number of days (default 30): ") or 30)
                self.plot_endpoint_analysis(days)
            elif choice == '3':
                days = int(input("Number of days (default 30): ") or 30)
                self.plot_content_discovery(days)
            elif choice == '4':
                days = int(input("Number of days (default 30): ") or 30)
                self.analyze_content_patterns(days)
            elif choice == '5':
                self.export_weekly_report()
            elif choice == '6':
                days = int(input("Number of days to analyze: "))
                print(f"\nGenerating analysis for last {days} days...")
                self.plot_activity_timeline(days)
                self.plot_endpoint_analysis(days)
                self.analyze_content_patterns(days)
            elif choice == '7':
                self.show_database_stats()
            else:
                print("Invalid option. Please try again.")
    
    def show_database_stats(self):
        """Show database statistics"""
        with self.get_connection() as conn:
            print("\n📊 Database Statistics")
            print("-" * 40)
            
            # Total records
            total_changes = conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
            total_content = conn.execute("SELECT COUNT(*) FROM discovered_content").fetchone()[0]
            
            print(f"Total Changes Recorded: {total_changes}")
            print(f"Total Content Items: {total_content}")
            
            # Date range
            date_range = conn.execute("""
                SELECT MIN(timestamp) as first, MAX(timestamp) as last
                FROM changes
            """).fetchone()
            
            if date_range[0]:
                print(f"Data Range: {date_range[0][:10]} to {date_range[1][:10]}")
            
            # Top significance scores
            top_scores = conn.execute("""
                SELECT endpoint, significance_score, timestamp
                FROM changes 
                ORDER BY significance_score DESC
                LIMIT 5
            """).fetchall()
            
            print(f"\nTop Significance Scores:")
            for endpoint, score, timestamp in top_scores:
                print(f"  • {score}: {endpoint} ({timestamp[:19]})")

def main():
    parser = argparse.ArgumentParser(description="EA FC DataMiner Analysis Dashboard")
    parser.add_argument("--db", default="data/ea_fc_changes.db", help="Database path")
    parser.add_argument("--days", type=int, default=30, help="Days to analyze")
    parser.add_argument("--report", action="store_true", help="Generate report only")
    
    args = parser.parse_args()
    
    analyzer = EAFCAnalyzer(args.db)
    
    if not analyzer.db_path.exists():
        print(f"❌ Database not found: {analyzer.db_path}")
        print("Make sure the main dataminer has been running to collect data.")
        return
    
    if args.report:
        # Just generate report and exit
        analyzer.export_weekly_report()
    else:
        # Interactive mode
        analyzer.interactive_analysis()

if __name__ == "__main__":
    main()
