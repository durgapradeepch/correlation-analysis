#!/usr/bin/env python3
"""
Alert Analysis Engine - Single executable that processes alerts and generates insights.

Reads alerts from a folder, normalizes + enriches + noise-cuts,
builds episodes → situations with temporal spread,
runs burst, PMI, and lead–lag correlations,
selects a primary cause (with path gating) and scores it,
writes only public/vl_insights.jsonl (NDJSON).
"""

import argparse
import json
import os
import sys
import hashlib
import re
from collections import defaultdict, Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any, Union
import math
import statistics
from itertools import combinations

import numpy as np
from scipy import stats
from dateutil import parser as date_parser


# Constants
PAD_MS_START = 60_000
MAX_PAD_MS = 600_000
BIN_SIZE_S_DEFAULT = 1
BIN_SIZE_S_FALLBACK = 5
MIN_SITUATION_MS = 10_000
MIN_BINS = 3
JOIN_HALO_MS = 300_000  # 5 min


class UnionFind:
    """Union-Find data structure for merging episodes into situations."""
    
    def __init__(self):
        self.parent = {}
        self.rank = {}
    
    def find(self, x):
        if x not in self.parent:
            self.parent[x] = x
            self.rank[x] = 0
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]
    
    def union(self, x, y):
        px, py = self.find(x), self.find(y)
        if px == py:
            return
        if self.rank[px] < self.rank[py]:
            px, py = py, px
        self.parent[py] = px
        if self.rank[px] == self.rank[py]:
            self.rank[px] += 1


class AlertEngine:
    """Main engine for processing alerts and generating insights."""
    
    def __init__(self, args):
        self.args = args
        self.graph = self._load_graph()
        self.raw_alerts = []  # Track original alerts for statistics
        self.alerts = []
        self.episodes = []
        self.situations = []
        self.correlations = []

        # Noise reduction tracking
        self.dedup_cache = {}  # (fingerprint, severity, entity_key) -> timestamp
        self.flap_tracker = defaultdict(list)  # (fingerprint, entity_key) -> [(ts, status), ...]
        self.echo_tracker = defaultdict(list)  # (fingerprint, entity_key) -> [(ts, source), ...]
    
    def _load_graph(self) -> Dict[str, List[str]]:
        """Load dependency graph if provided."""
        if not self.args.graph or not os.path.exists(self.args.graph):
            return {}
        
        try:
            with open(self.args.graph, 'r') as f:
                data = json.load(f)
                return data.get('adj', {})
        except Exception as e:
            print(f"Warning: Failed to load graph from {self.args.graph}: {e}")
            return {}
    
    def _parse_timestamp(self, ts_value) -> int:
        """Parse various timestamp formats to epoch milliseconds."""
        if isinstance(ts_value, (int, float)):
            # Assume seconds if < 1e12, else milliseconds
            if ts_value < 1e12:
                return int(ts_value * 1000)
            return int(ts_value)
        
        if isinstance(ts_value, str):
            try:
                dt = date_parser.parse(ts_value)
                return int(dt.timestamp() * 1000)
            except:
                pass
        
        # Fallback to current time
        return int(datetime.now(timezone.utc).timestamp() * 1000)
    
    def _extract_tags(self, tags_list) -> Dict[str, Union[str, int, bool]]:
        """Extract tags from Datadog format to flat dict."""
        tags = {}
        if not isinstance(tags_list, list):
            return tags
        
        for tag in tags_list:
            if isinstance(tag, str) and ':' in tag:
                key, value = tag.split(':', 1)
                # Try to convert to appropriate type
                if value.lower() in ('true', 'false'):
                    tags[key] = value.lower() == 'true'
                elif value.isdigit():
                    tags[key] = int(value)
                else:
                    tags[key] = value
            elif isinstance(tag, str):
                tags[tag] = True
        
        return tags
    
    def _generate_fingerprint(self, alert: Dict) -> str:
        """Generate stable service-level fingerprint.

        Aggregates at service/deployment/namespace level to avoid pod-level fragmentation.
        Excludes volatile fields like pod_name, resource_id, vendor_event_id.
        """
        # Simple service-level fingerprint - no volatile fields
        fp_string = (
            f"title={alert.get('title', '')}"
            f"|sev={alert.get('severity', '')}"
            f"|cluster={alert.get('cluster', '')}"
            f"|ns={alert.get('ns', '')}"
            f"|service={alert.get('service', '')}"
        )

        return hashlib.sha256(fp_string.encode()).hexdigest()
    
    def _normalize_datadog_alert(self, raw_alert: Dict) -> Dict:
        """Normalize Datadog alert to internal schema."""
        metadata = raw_alert.get('metadata', {})
        event = metadata.get('event', {})
        attrs = event.get('attributes', {})
        
        # Extract timestamp
        ts = self._parse_timestamp(
            attrs.get('timestamp') or 
            raw_alert.get('first_seen') or 
            raw_alert.get('created_at')
        )
        
        # Extract tags
        tags = self._extract_tags(attrs.get('tags', []))
        
        # Determine status
        status = "firing"
        current_status = raw_alert.get('current_status', '').lower()
        if current_status in ['ok', 'resolved']:
            status = "resolved"
        elif current_status in ['no data', 'error']:
            status = "firing"
        
        # Extract service and infrastructure info
        service = tags.get('service', 'undefined')
        cluster = tags.get('kube_cluster_name', tags.get('cluster'))
        ns = tags.get('kube_namespace', tags.get('namespace'))
        pod = tags.get('pod_name', tags.get('pod'))
        host = tags.get('host')
        
        # Determine entity_key (strongest available)
        entity_key = "entity:na"
        if service and service != "undefined":
            entity_key = f"svc:{service}"
        elif ns and pod:
            entity_key = f"pod:{pod}"
        elif host:
            entity_key = f"host:{host}"
        elif cluster:
            entity_key = f"cluster:{cluster}"
        
        # Build normalized alert
        alert = {
            'ts': ts,
            'source': 'datadog',
            'vendor_event_id': event.get('id', str(raw_alert.get('id', ''))),
            'resource_id': (
                attrs.get('event_object') or 
                event.get('id') or 
                attrs.get('aggregation_key') or 
                f"{raw_alert.get('id', '')}|{attrs.get('group_key', '')}"
            ),
            'fingerprint': '',  # Will be set after creation
            'status': status,
            'severity': 'high',  # Default, can be upgraded to critical
            'kind': 'alert',
            'title': attrs.get('message', '').split('\n')[0] if attrs.get('message') else None,
            'service': service,
            'component': None,
            'resource': None,
            'env': tags.get('env'),
            'region': tags.get('region'),
            'cluster': cluster,
            'ns': ns,
            'pod': pod,
            'host': host,
            'error_code': tags.get('error_code'),
            'tags': tags,
            'entity_key': entity_key,
            'deploy_key': tags.get('git_sha') or tags.get('release') or tags.get('commit'),
            'net_key': None,
            'k8s_key': f"{cluster or ''}/{ns or ''}/{pod or ''}",
            'urls': None
        }
        
        # Set net_key if network info available
        src_ip = tags.get('src_ip')
        dst_ip = tags.get('dst_ip')
        src_host = tags.get('src_host')
        dst_host = tags.get('dst_host')
        
        if src_ip and dst_ip:
            alert['net_key'] = f"{src_ip}→{dst_ip}"
        elif src_host and dst_host:
            alert['net_key'] = f"{src_host}→{dst_host}"
        
        # Generate fingerprint
        alert['fingerprint'] = self._generate_fingerprint(alert)
        
        # Fallback resource_id if empty
        if not alert['resource_id']:
            alert['resource_id'] = hashlib.sha256(
                f"{alert['source']}|{alert['vendor_event_id']}|{alert['entity_key']}".encode()
            ).hexdigest()
        
        return alert

    def _apply_noise_cut(self, alerts: List[Dict]) -> List[Dict]:
        """Apply dedup TTL, flap guard, and vendor echo suppression."""
        filtered_alerts = []
        current_time = max(alert['ts'] for alert in alerts) if alerts else 0

        for alert in sorted(alerts, key=lambda x: x['ts']):
            # Dedup TTL check
            dedup_key = (alert['fingerprint'], alert['severity'], alert['entity_key'])
            if dedup_key in self.dedup_cache:
                if alert['ts'] - self.dedup_cache[dedup_key] < self.args.dedup_ttl * 1000:
                    continue  # Skip duplicate
            self.dedup_cache[dedup_key] = alert['ts']

            # Vendor echo suppression
            echo_key = (alert['fingerprint'], alert['entity_key'])
            echo_window = 10 * 1000  # 10 seconds

            # Check for recent alerts from other sources
            is_echo = False
            for prev_ts, prev_source in self.echo_tracker[echo_key]:
                if alert['ts'] - prev_ts <= echo_window and prev_source != alert['source']:
                    is_echo = True
                    break

            if not is_echo:
                self.echo_tracker[echo_key].append((alert['ts'], alert['source']))
                # Clean old entries
                self.echo_tracker[echo_key] = [
                    (ts, src) for ts, src in self.echo_tracker[echo_key]
                    if alert['ts'] - ts <= echo_window
                ]

                # Track flapping for scoring
                flap_key = (alert['fingerprint'], alert['entity_key'])
                self.flap_tracker[flap_key].append((alert['ts'], alert['status']))

                # Clean old flap entries (10 min window)
                flap_window = 10 * 60 * 1000
                self.flap_tracker[flap_key] = [
                    (ts, status) for ts, status in self.flap_tracker[flap_key]
                    if alert['ts'] - ts <= flap_window
                ]

                filtered_alerts.append(alert)

        return filtered_alerts

    def _calculate_flap_score(self, fingerprint: str, entity_key: str) -> float:
        """Calculate flap score for an entity/fingerprint pair."""
        flap_key = (fingerprint, entity_key)
        if flap_key not in self.flap_tracker:
            return 0.0

        statuses = [status for _, status in self.flap_tracker[flap_key]]
        if len(statuses) < 2:
            return 0.0

        # Count status toggles
        toggles = 0
        for i in range(1, len(statuses)):
            if statuses[i] != statuses[i-1]:
                toggles += 1

        # Normalize to [0, 0.3]
        return min(0.3, toggles / len(statuses))

    def _build_episodes(self, alerts: List[Dict]) -> List[Dict]:
        """Group alerts into episodes by entity_key and fingerprint."""
        # Group by (entity_key, fingerprint)
        groups = defaultdict(list)
        for alert in alerts:
            key = (alert['entity_key'], alert['fingerprint'])
            groups[key].append(alert)

        episodes = []
        for (entity_key, fingerprint), group_alerts in groups.items():
            # Sort by timestamp
            group_alerts.sort(key=lambda x: x['ts'])

            # Split into episodes based on gap
            current_episode = []

            for alert in group_alerts:
                if (current_episode and
                    alert['ts'] - current_episode[-1]['ts'] > self.args.episode_gap * 1000):
                    # Gap too large, finish current episode
                    if current_episode:
                        episodes.append(self._create_episode(current_episode))
                    current_episode = [alert]
                else:
                    current_episode.append(alert)

            # Add final episode
            if current_episode:
                episodes.append(self._create_episode(current_episode))

        return episodes

    def _create_episode(self, alerts: List[Dict]) -> Dict:
        """Create episode object from list of alerts."""
        alerts.sort(key=lambda x: x['ts'])

        # Sample arrays to avoid memory issues
        vendor_event_ids = list(set(alert['vendor_event_id'] for alert in alerts))[:50]
        resource_ids = list(set(alert['resource_id'] for alert in alerts))[:50]
        sample_ts = [alert['ts'] for alert in alerts][:50]

        deploy_keys = list(set(
            alert['deploy_key'] for alert in alerts
            if alert.get('deploy_key')
        ))

        net_keys = list(set(
            alert['net_key'] for alert in alerts
            if alert.get('net_key')
        ))

        vendors = list(set(alert['source'] for alert in alerts))

        return {
            'entity_key': alerts[0]['entity_key'],
            'fingerprint': alerts[0]['fingerprint'],
            'start': alerts[0]['ts'],
            'end': alerts[-1]['ts'],
            'count': len(alerts),
            'vendors': vendors,
            'vendor_event_ids': vendor_event_ids,
            'resource_ids': resource_ids,
            'deploy_keys': deploy_keys,
            'net_keys': net_keys,
            'sample_ts': sample_ts,
            'alerts': alerts  # Keep for reference
        }

    def _build_situations(self, episodes: List[Dict]) -> List[Dict]:
        """Merge episodes into situations using union-find."""
        if not episodes:
            return []

        uf = UnionFind()

        # Initialize all episodes
        for i, ep in enumerate(episodes):
            uf.find(i)

        # Check all pairs for overlap and key matches
        for i, ep1 in enumerate(episodes):
            for j, ep2 in enumerate(episodes):
                if i >= j:
                    continue

                # Check temporal overlap with halo
                overlap = not ((ep1['end'] + JOIN_HALO_MS) < ep2['start'] or
                              (ep2['end'] + JOIN_HALO_MS) < ep1['start'])

                if overlap:
                    # Check for key matches
                    should_merge = False

                    # Same entity_key or fingerprint
                    if (ep1['entity_key'] == ep2['entity_key'] or
                        ep1['fingerprint'] == ep2['fingerprint']):
                        should_merge = True

                    # Intersecting deploy_keys
                    if (ep1['deploy_keys'] and ep2['deploy_keys'] and
                        set(ep1['deploy_keys']) & set(ep2['deploy_keys'])):
                        should_merge = True

                    # Intersecting net_keys
                    if (ep1['net_keys'] and ep2['net_keys'] and
                        set(ep1['net_keys']) & set(ep2['net_keys'])):
                        should_merge = True

                    # Graph edge between entities
                    if self.graph:
                        entity1 = ep1['entity_key']
                        entity2 = ep2['entity_key']
                        if (entity1 in self.graph and entity2 in self.graph.get(entity1, []) or
                            entity2 in self.graph and entity1 in self.graph.get(entity2, [])):
                            should_merge = True

                    if should_merge:
                        uf.union(i, j)

        # Group episodes by their root
        situation_groups = defaultdict(list)
        for i, ep in enumerate(episodes):
            root = uf.find(i)
            situation_groups[root].append(ep)

        # Create situation objects
        situations = []
        for group_episodes in situation_groups.values():
            situation = self._create_situation(group_episodes)
            situations.append(situation)

        return situations

    def _create_situation(self, episodes: List[Dict]) -> Optional[Dict]:
        """Create situation object from list of episodes."""
        if not episodes:
            return None

        # Calculate situation window
        start = min(ep['start'] for ep in episodes)
        end = max(ep['end'] for ep in episodes)

        # Generate situation ID
        situation_id = f"S-{start}-{end}-{len(episodes)}"

        # Collect all alerts from episodes
        all_alerts = []
        for ep in episodes:
            all_alerts.extend(ep.get('alerts', []))

        # Calculate blast radius
        unique_entities = set(ep['entity_key'] for ep in episodes)
        unique_services = set()
        for ep in episodes:
            for alert in ep.get('alerts', []):
                if alert.get('service') and alert['service'] != 'undefined':
                    unique_services.add(alert['service'])

        # Collect resource references
        resource_refs = []
        seen_resources = set()
        for ep in episodes:
            for resource_id in ep.get('resource_ids', []):
                if resource_id not in seen_resources:
                    seen_resources.add(resource_id)
                    # Find an alert with this resource_id to get URLs
                    alert_with_resource = None
                    for alert in ep.get('alerts', []):
                        if alert['resource_id'] == resource_id:
                            alert_with_resource = alert
                            break

                    resource_refs.append({
                        'source': ep.get('alerts', [{}])[0].get('source', 'unknown'),
                        'resource_id': resource_id,
                        'urls': alert_with_resource.get('urls') if alert_with_resource else None
                    })

        # Collect change references
        change_refs = []
        for ep in episodes:
            for deploy_key in ep.get('deploy_keys', []):
                if deploy_key:
                    change_refs.append({
                        'type': 'deploy',
                        'sha': deploy_key,
                        'started_at': datetime.fromtimestamp(ep['start'] / 1000, timezone.utc).isoformat()
                    })

        # Collect related alerts (sample up to 200)
        related_alerts = []
        for alert in all_alerts[:200]:
            related_alerts.append({
                'ts': alert['ts'],
                'entity_key': alert['entity_key'],
                'fingerprint': alert['fingerprint'],
                'vendor_event_id': alert['vendor_event_id'],
                'resource_id': alert['resource_id']
            })

        return {
            'situation_id': situation_id,
            'window': {'start': start, 'end': end},
            'episodes': [
                {
                    'entity_key': ep['entity_key'],
                    'fingerprint': ep['fingerprint'],
                    'start': ep['start'],
                    'end': ep['end'],
                    'count': ep['count']
                }
                for ep in episodes
            ],
            'blast_radius': {
                'entities': len(unique_entities),
                'services': len(unique_services)
            },
            'change_refs': change_refs,
            'resource_refs': resource_refs,
            'related_alerts': related_alerts,
            'all_alerts': all_alerts,  # Keep for correlation analysis
            'raw_episodes': episodes,  # Keep for analysis
            'insufficient_temporal_spread': False,
            'reason': None,
            'pad_ms_used': PAD_MS_START,
            'bin_size_s': BIN_SIZE_S_DEFAULT
        }

    def _apply_temporal_spread(self, situation: Dict) -> Dict:
        """Apply temporal spread with padding, rehydration, and dynamic binning."""
        start = situation['window']['start']
        end = situation['window']['end']

        # Quick check for minimum duration
        if end - start < MIN_SITUATION_MS:
            situation['insufficient_temporal_spread'] = True
            situation['reason'] = f"Situation duration {end - start}ms < minimum {MIN_SITUATION_MS}ms"
            return situation

        # Start with initial padding
        pad_ms = PAD_MS_START

        # Pre-filter alerts that could match this situation for efficiency
        relevant_alerts = []
        situation_keys = set()
        for ep in situation['raw_episodes']:
            situation_keys.add(ep['entity_key'])
            situation_keys.add(ep['fingerprint'])
            situation_keys.update(ep.get('deploy_keys', []))
            situation_keys.update(ep.get('net_keys', []))

        for alert in self.alerts:
            if (alert['entity_key'] in situation_keys or
                alert['fingerprint'] in situation_keys or
                (alert.get('deploy_key') and alert['deploy_key'] in situation_keys) or
                (alert.get('net_key') and alert['net_key'] in situation_keys)):
                relevant_alerts.append(alert)

        while pad_ms <= MAX_PAD_MS:
            padded_start = start - pad_ms
            padded_end = end + pad_ms

            # Rehydrate alerts in padded window
            rehydrated_alerts = [
                alert for alert in relevant_alerts
                if padded_start <= alert['ts'] <= padded_end
            ]

            if not rehydrated_alerts:
                pad_ms *= 2
                continue

            # Try binning
            bin_size_s = BIN_SIZE_S_DEFAULT
            bins = self._create_bins(rehydrated_alerts, padded_start, padded_end, bin_size_s)

            # Count distinct bins with data
            distinct_bins = 0
            for series_bins in bins.values():
                if any(count > 0 for count in series_bins):
                    distinct_bins += 1

            if distinct_bins < MIN_BINS:
                # Try fallback bin size
                bin_size_s = BIN_SIZE_S_FALLBACK
                bins = self._create_bins(rehydrated_alerts, padded_start, padded_end, bin_size_s)
                distinct_bins = 0
                for series_bins in bins.values():
                    if any(count > 0 for count in series_bins):
                        distinct_bins += 1

            # Check if we have enough bins and duration
            duration_ms = padded_end - padded_start
            if distinct_bins >= MIN_BINS and duration_ms >= MIN_SITUATION_MS:
                situation['pad_ms_used'] = pad_ms
                situation['bin_size_s'] = bin_size_s
                situation['bins'] = bins
                situation['padded_window'] = {'start': padded_start, 'end': padded_end}
                situation['rehydrated_alerts'] = rehydrated_alerts
                return situation

            # Double padding and try again
            pad_ms *= 2

        # Mark as degenerate
        situation['insufficient_temporal_spread'] = True
        situation['reason'] = f"Could not achieve {MIN_BINS} distinct bins or {MIN_SITUATION_MS}ms duration"
        return situation

    def _create_bins(self, alerts: List[Dict], start_ms: int, end_ms: int, bin_size_s: int) -> Dict:
        """Create time bins with alert counts per series."""
        bin_size_ms = bin_size_s * 1000
        num_bins = int((end_ms - start_ms) / bin_size_ms) + 1

        # Initialize bins
        bins = {}
        series_keys = set()

        # Collect all series keys (use fingerprint only for service-level aggregation)
        for alert in alerts:
            series_key = alert['fingerprint']  # Service-level aggregation
            series_keys.add(series_key)

        # Initialize all series in all bins
        for series_key in series_keys:
            bins[series_key] = [0] * num_bins

        # Fill bins with alert counts
        for alert in alerts:
            bin_idx = int((alert['ts'] - start_ms) / bin_size_ms)
            if 0 <= bin_idx < num_bins:
                series_key = alert['fingerprint']  # Service-level aggregation
                bins[series_key][bin_idx] += 1

        return bins

    def _run_correlations(self, situation: Dict) -> List[Dict]:
        """Run burst, PMI, and lead-lag correlations for a situation."""
        if situation.get('insufficient_temporal_spread'):
            return []

        bins = situation.get('bins', {})
        if not bins:
            return []

        correlations = []
        series_keys = list(bins.keys())

        # Limit the number of series to avoid combinatorial explosion
        MAX_SERIES = 400  # Increased for service-level aggregation
        if len(series_keys) > MAX_SERIES:
            print(f"  Warning: Too many series ({len(series_keys)}), limiting to {MAX_SERIES}")
            # Keep the most active series
            series_activity = []
            for key in series_keys:
                activity = sum(bins[key])
                series_activity.append((activity, key))
            series_activity.sort(reverse=True)
            series_keys = [key for _, key in series_activity[:MAX_SERIES]]

        # Limit the number of pairs to process
        MAX_PAIRS = 20000  # Increased for better correlation coverage
        pairs = list(combinations(series_keys, 2))
        if len(pairs) > MAX_PAIRS:
            print(f"  Warning: Too many pairs ({len(pairs)}), limiting to {MAX_PAIRS}")
            pairs = pairs[:MAX_PAIRS]

        print(f"  Processing {len(pairs)} correlation pairs...")

        # Run all correlation methods
        for i, (series_a, series_b) in enumerate(pairs):
            if i % 100 == 0 and i > 0:
                print(f"    Processed {i}/{len(pairs)} pairs...")

            # Burst correlation
            burst_corr = self._burst_correlation(bins[series_a], bins[series_b], series_a, series_b)
            if burst_corr:
                correlations.append({
                    'type': 'correlation',
                    'method': 'burst',
                    'situation_id': situation['situation_id'],
                    'series_a': series_a,
                    'series_b': series_b,
                    'window': situation['padded_window'],
                    'metrics': {'burst': burst_corr},
                    'resource_ids_a': self._get_resource_ids_for_series(situation, series_a),
                    'resource_ids_b': self._get_resource_ids_for_series(situation, series_b)
                })

            # PMI correlation
            pmi_corr = self._pmi_correlation(bins[series_a], bins[series_b])
            if pmi_corr:
                correlations.append({
                    'type': 'correlation',
                    'method': 'pmi',
                    'situation_id': situation['situation_id'],
                    'series_a': series_a,
                    'series_b': series_b,
                    'window': situation['padded_window'],
                    'metrics': {'pmi': pmi_corr},
                    'resource_ids_a': self._get_resource_ids_for_series(situation, series_a),
                    'resource_ids_b': self._get_resource_ids_for_series(situation, series_b)
                })

            # Lead-lag correlation
            leadlag_corr = self._leadlag_correlation(bins[series_a], bins[series_b])
            if leadlag_corr:
                correlations.append({
                    'type': 'correlation',
                    'method': 'leadlag',
                    'situation_id': situation['situation_id'],
                    'series_a': series_a,
                    'series_b': series_b,
                    'window': situation['padded_window'],
                    'metrics': {'leadlag': leadlag_corr},
                    'resource_ids_a': self._get_resource_ids_for_series(situation, series_a),
                    'resource_ids_b': self._get_resource_ids_for_series(situation, series_b)
                })

        return correlations

    def _get_resource_ids_for_series(self, situation: Dict, series_key: str) -> List[str]:
        """Get resource IDs for a specific series (now fingerprint-based)."""
        fingerprint = series_key  # series_key is now just the fingerprint
        resource_ids = []

        for ep in situation.get('raw_episodes', []):
            if ep['fingerprint'] == fingerprint:
                resource_ids.extend(ep.get('resource_ids', []))

        return list(set(resource_ids))[:10]  # Limit to avoid bloat

    def _burst_correlation(self, series_a: List[int], series_b: List[int],
                          key_a: str, key_b: str) -> Optional[Dict]:
        """Calculate burst correlation between two series."""
        # Note: key_a and key_b are kept for potential future use
        if len(series_a) != len(series_b) or len(series_a) < 3:
            return None

        # Calculate burst thresholds
        def get_burst_threshold(series):
            if not series or max(series) == 0:
                return float('inf')
            median = statistics.median(series)
            mad = statistics.median([abs(x - median) for x in series])
            return median + 3 * mad

        threshold_a = get_burst_threshold(series_a)
        threshold_b = get_burst_threshold(series_b)

        # Find bursts
        bursts_a = [i for i, x in enumerate(series_a) if x > threshold_a]
        bursts_b = [i for i, x in enumerate(series_b) if x > threshold_b]

        if not bursts_a or not bursts_b:
            return None

        # Count aligned bursts (within ±1 bin)
        aligned = 0
        for burst_a in bursts_a:
            for burst_b in bursts_b:
                if abs(burst_a - burst_b) <= 1:
                    aligned += 1
                    break

        if aligned < self.args.min_support:
            return None

        # Calculate burst score
        burst_score = aligned / math.sqrt(len(bursts_a) * len(bursts_b))

        if burst_score >= 0.2:
            return {
                'aligned': aligned,
                'score': burst_score
            }

        return None

    def _pmi_correlation(self, series_a: List[int], series_b: List[int]) -> Optional[Dict]:
        """Calculate PMI co-occurrence correlation."""
        if len(series_a) != len(series_b) or len(series_a) < 3:
            return None

        # Convert to binary (active/inactive)
        active_a = [1 if x > 0 else 0 for x in series_a]
        active_b = [1 if x > 0 else 0 for x in series_b]

        # Count co-occurrences with add-one smoothing
        co_count = sum(1 for a, b in zip(active_a, active_b) if a and b)
        count_a = sum(active_a)
        count_b = sum(active_b)
        total = len(series_a)

        if co_count < self.args.min_support:
            return None

        # Add-one smoothing
        co_count += 1
        count_a += 1
        count_b += 1
        total += 4  # Add 4 for the 2x2 contingency table

        # Calculate PMI
        p_ab = co_count / total
        p_a = count_a / total
        p_b = count_b / total

        if p_a == 0 or p_b == 0:
            return None

        pmi = math.log2(p_ab / (p_a * p_b))

        if pmi >= 1.0:
            return {
                'pmi': pmi,
                'co_count': co_count - 1  # Remove smoothing for reporting
            }

        return None

    def _leadlag_correlation(self, series_a: List[int], series_b: List[int]) -> Optional[Dict]:
        """Calculate lead-lag correlation using cross-correlation."""
        if len(series_a) != len(series_b) or len(series_a) < 3:
            return None

        # Convert to impulse trains (0/1)
        impulse_a = [1 if x > 0 else 0 for x in series_a]
        impulse_b = [1 if x > 0 else 0 for x in series_b]

        # Calculate cross-correlation for different lags
        max_lag_bins = min(self.args.max_lag, len(impulse_a) - 1)
        best_score = 0
        best_lag = 0

        for lag in range(max_lag_bins + 1):
            if lag == 0:
                # No lag
                aligned = sum(a * b for a, b in zip(impulse_a, impulse_b))
                total_a = sum(impulse_a)
                total_b = sum(impulse_b)
            else:
                # Positive lag: a leads b
                aligned = sum(impulse_a[i] * impulse_b[i + lag]
                             for i in range(len(impulse_a) - lag))
                total_a = sum(impulse_a[:-lag])
                total_b = sum(impulse_b[lag:])

            if total_a == 0 or total_b == 0:
                continue

            # Normalized cross-correlation
            score = aligned / math.sqrt(total_a * total_b)

            if score > best_score:
                best_score = score
                best_lag = lag

        if best_score >= 0.3 and sum(impulse_a) >= 2 and sum(impulse_b) >= 2:
            return {
                'lag_ms': best_lag * 1000,  # Convert to milliseconds
                'score': best_score
            }

        return None

    def _select_primary_cause(self, situation: Dict) -> Dict:
        """Select primary cause and calculate confidence score."""
        episodes = situation.get('raw_episodes', [])
        if not episodes:
            return situation

        # Find earliest episode as candidate
        earliest_episode = min(episodes, key=lambda ep: ep['start'])

        # Check path gating
        has_path = self._check_dependency_path(earliest_episode, episodes)

        # Calculate composite score
        score_components = self._calculate_score_components(earliest_episode, situation, has_path)

        composite_score = (
            0.35 * score_components['change_proximity'] +
            0.20 * score_components['lead_lag'] +
            0.20 * score_components['graph_path'] +
            0.15 * score_components['cardinality'] +
            0.15 * score_components['severity'] -
            0.10 * score_components['flap'] -
            0.05 * score_components['echo']
        )

        confidence = min(1.0, max(0.0, composite_score))

        # Apply path gating
        if not has_path and self.graph:
            confidence = min(confidence, 0.35)

        situation['primary_cause'] = {
            'entity': earliest_episode['entity_key'],
            'fingerprint': earliest_episode['fingerprint'],
            'confidence': confidence,
            'lag_ms': 0  # Primary cause has no lag by definition
        }

        situation['score'] = confidence

        # Generate next actions based on confidence and type
        situation['next_actions'] = self._generate_next_actions(earliest_episode, confidence)

        return situation

    def _check_dependency_path(self, cause_episode: Dict, all_episodes: List[Dict]) -> bool:
        """Check if there's a dependency path from cause to other episodes."""
        if not self.graph:
            return True  # No graph provided, assume path exists

        cause_entity = cause_episode['entity_key']

        # BFS to find paths to other entities
        for episode in all_episodes:
            if episode == cause_episode:
                continue

            target_entity = episode['entity_key']
            if self._has_path_bfs(cause_entity, target_entity):
                return True

        return False

    def _has_path_bfs(self, start: str, target: str) -> bool:
        """BFS to check if path exists from start to target."""
        if start == target:
            return True

        visited = set()
        queue = [start]

        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)

            if current == target:
                return True

            # Add neighbors
            for neighbor in self.graph.get(current, []):
                if neighbor not in visited:
                    queue.append(neighbor)

        return False

    def _calculate_score_components(self, episode: Dict, situation: Dict, has_path: bool) -> Dict:
        """Calculate individual score components."""
        components = {
            'change_proximity': 0.0,
            'lead_lag': 0.0,
            'graph_path': 0.0,
            'cardinality': 0.0,
            'severity': 0.0,
            'flap': 0.0,
            'echo': 0.0
        }

        # Change proximity
        if episode.get('deploy_keys'):
            # Assume change happened at episode start for simplicity
            time_since_change = 0  # minutes
            if time_since_change <= 5:
                components['change_proximity'] = 1.0
            elif time_since_change <= 15:
                components['change_proximity'] = 0.7
            else:
                components['change_proximity'] = 0.2

        # Graph path
        if has_path and self.graph:
            # Simplified: assume path length of 1 for now
            components['graph_path'] = 1.0 / (1 + 1)

        # Cardinality
        unique_entities = len(set(ep['entity_key'] for ep in situation.get('raw_episodes', [])))
        components['cardinality'] = math.log(max(1, unique_entities)) / math.log(10)  # Normalize

        # Severity (max over all alerts in episode)
        max_severity = 'low'
        for alert in episode.get('alerts', []):
            severity = alert.get('severity', 'low')
            if severity == 'critical':
                max_severity = 'critical'
                break
            elif severity == 'high' and max_severity != 'critical':
                max_severity = 'high'
            elif severity == 'medium' and max_severity not in ['critical', 'high']:
                max_severity = 'medium'

        severity_scores = {'low': 0.25, 'medium': 0.5, 'high': 0.75, 'critical': 1.0}
        components['severity'] = severity_scores.get(max_severity, 0.25)

        # Flap penalty
        components['flap'] = self._calculate_flap_score(episode['fingerprint'], episode['entity_key'])

        # Echo penalty (simplified)
        echo_key = (episode['fingerprint'], episode['entity_key'])
        if echo_key in self.echo_tracker and len(self.echo_tracker[echo_key]) > 1:
            components['echo'] = 0.3

        return components

    def _generate_next_actions(self, episode: Dict, confidence: float) -> List[str]:
        """Generate recommended next actions."""
        actions = []

        if confidence > 0.8:
            if episode.get('deploy_keys'):
                actions.append(f"rollback deployment {episode['deploy_keys'][0][:8]}")
            actions.append("page oncall team")
        elif confidence > 0.5:
            actions.append("investigate root cause")
            actions.append("check recent changes")
        else:
            actions.append("monitor situation")
            actions.append("gather more data")

        return actions

    def load_alerts(self) -> List[Dict]:
        """Load all alerts from input directory."""
        alerts = []
        input_path = Path(self.args.input)

        if not input_path.exists():
            print(f"Error: Input path {input_path} does not exist")
            return alerts

        # Handle single file or directory
        if input_path.is_file():
            alerts.extend(self._load_file(input_path))
        else:
            # Load all JSON files in directory
            for file_path in input_path.glob('*.json'):
                alerts.extend(self._load_file(file_path))
            for file_path in input_path.glob('*.jsonl'):
                alerts.extend(self._load_file(file_path))
            for file_path in input_path.glob('*.ndjson'):
                alerts.extend(self._load_file(file_path))

        print(f"Loaded {len(alerts)} raw alerts")
        return alerts

    def _load_file(self, file_path: Path) -> List[Dict]:
        """Load alerts from a single file."""
        alerts = []

        try:
            with open(file_path, 'r') as f:
                if file_path.suffix == '.json':
                    # JSON array format
                    data = json.load(f)
                    if isinstance(data, dict) and 'data' in data:
                        alerts.extend(data['data'])
                    elif isinstance(data, list):
                        alerts.extend(data)
                    else:
                        alerts.append(data)
                else:
                    # JSONL/NDJSON format
                    for line in f:
                        line = line.strip()
                        if line:
                            alerts.append(json.loads(line))

        except Exception as e:
            print(f"Warning: Failed to load {file_path}: {e}")

        return alerts

    def normalize_alerts(self, raw_alerts: List[Dict]) -> List[Dict]:
        """Normalize all alerts to internal schema."""
        normalized = []

        for raw_alert in raw_alerts:
            try:
                # Detect source and normalize accordingly
                if 'metadata' in raw_alert and 'event' in raw_alert['metadata']:
                    # Datadog format
                    alert = self._normalize_datadog_alert(raw_alert)
                    normalized.append(alert)
                else:
                    # Unknown format, skip or add basic normalization
                    print(f"Warning: Unknown alert format, skipping: {raw_alert.get('id', 'unknown')}")

            except Exception as e:
                print(f"Warning: Failed to normalize alert: {e}")

        print(f"Normalized {len(normalized)} alerts")
        return normalized

    def run(self):
        """Main execution flow."""
        print("Starting Alert Analysis Engine...")

        # 1. Load alerts
        raw_alerts = self.load_alerts()
        if not raw_alerts:
            print("No alerts found, exiting")
            return

        # Store raw alerts for statistics
        self.raw_alerts = raw_alerts

        # 2. Normalize alerts
        self.alerts = self.normalize_alerts(raw_alerts)
        if not self.alerts:
            print("No valid alerts after normalization, exiting")
            return

        # 3. Apply noise cut
        print("Applying noise reduction...")
        filtered_alerts = self._apply_noise_cut(self.alerts)
        print(f"After noise cut: {len(filtered_alerts)} alerts (reduced from {len(self.alerts)})")

        # Update alerts to the filtered set for statistics
        self.alerts = filtered_alerts

        # 4. Build episodes
        print("Building episodes...")
        self.episodes = self._build_episodes(filtered_alerts)
        print(f"Created {len(self.episodes)} episodes")

        if len(self.episodes) >= len(filtered_alerts):
            print("Warning: Episodes >= alerts, check fingerprint logic or increase episode gap")

        # 5. Build situations
        print("Building situations...")
        raw_situations = self._build_situations(self.episodes)
        print(f"Created {len(raw_situations)} raw situations")

        # 6. Apply temporal spread and run correlations
        print("Processing situations...")
        self.situations = []
        self.correlations = []

        for i, situation in enumerate(raw_situations):
            if situation is None:
                continue

            print(f"Processing situation {i+1}/{len(raw_situations)}: {situation['situation_id']}")

            # Apply temporal spread
            situation = self._apply_temporal_spread(situation)

            # Run correlations if not degenerate
            if not situation.get('insufficient_temporal_spread'):
                correlations = self._run_correlations(situation)
                self.correlations.extend(correlations)
                print(f"  Found {len(correlations)} correlations")
            else:
                print(f"  Skipped correlations: {situation.get('reason', 'insufficient temporal spread')}")

            # Select primary cause
            situation = self._select_primary_cause(situation)

            self.situations.append(situation)

        print(f"Processed {len(self.situations)} situations")
        print(f"Found {len(self.correlations)} correlations")

        # 7. Write output
        self._write_output()
        print(f"Output written to {self.args.out}")

    def _write_output(self):
        """Write NDJSON output file."""
        output_dir = os.path.dirname(self.args.out)
        if output_dir:  # Only create directory if it's not empty (i.e., not current directory)
            os.makedirs(output_dir, exist_ok=True)

        with open(self.args.out, 'w') as f:
            # Write run metadata first
            run_meta = {
                'type': 'run_meta',
                'input_dir': self.args.input,
                'window_sec': self.args.window,
                'max_lag_sec': self.args.max_lag,
                'min_support': self.args.min_support,
                'dedup_ttl_sec': self.args.dedup_ttl,
                'episode_gap_sec': self.args.episode_gap,
                'raw_alerts': len(self.raw_alerts),
                'processed_alerts': len(self.alerts),
                'episodes_created': len(self.episodes),
                'situations_created': len(self.situations),
                'correlations_found': len(self.correlations),
                'generated_at': datetime.now(timezone.utc).isoformat()
            }
            f.write(json.dumps(run_meta) + '\n')

            # Write situations
            for situation in self.situations:
                output_situation = {
                    'type': 'situation',
                    'situation_id': situation['situation_id'],
                    'window': situation['window'],
                    'episodes': situation['episodes'],
                    'primary_cause': situation.get('primary_cause', {}),
                    'blast_radius': situation['blast_radius'],
                    'change_refs': situation['change_refs'],
                    'resource_refs': situation['resource_refs'],
                    'related_alerts': situation['related_alerts'],
                    'score': situation.get('score', 0.0),
                    'next_actions': situation.get('next_actions', []),
                    'insufficient_temporal_spread': situation.get('insufficient_temporal_spread', False),
                    'reason': situation.get('reason'),
                    'pad_ms_used': situation.get('pad_ms_used', PAD_MS_START),
                    'bin_size_s': situation.get('bin_size_s', BIN_SIZE_S_DEFAULT)
                }
                f.write(json.dumps(output_situation) + '\n')

            # Write correlations
            for correlation in self.correlations:
                f.write(json.dumps(correlation) + '\n')


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='Alert Analysis Engine')

    parser.add_argument('--input', required=True,
                       help='Input directory or file containing alerts')
    parser.add_argument('--out', required=True,
                       help='Output NDJSON file path')
    parser.add_argument('--window', type=int, default=900,
                       help='Analysis window in seconds (default: 900)')
    parser.add_argument('--hop', type=int, default=1,
                       help='Hop size in seconds (default: 1)')
    parser.add_argument('--dedup-ttl', type=int, default=120,
                       help='Deduplication TTL in seconds (default: 120)')
    parser.add_argument('--episode-gap', type=int, default=300,
                       help='Episode gap threshold in seconds (default: 300)')
    parser.add_argument('--max-lag', type=int, default=90,
                       help='Maximum lag for correlation in seconds (default: 90)')
    parser.add_argument('--min-support', type=int, default=3,
                       help='Minimum support for correlations (default: 3)')
    parser.add_argument('--graph', type=str,
                       help='Optional dependency graph JSON file')

    args = parser.parse_args()

    # Validate arguments
    if not os.path.exists(args.input):
        print(f"Error: Input path {args.input} does not exist")
        sys.exit(1)

    try:
        engine = AlertEngine(args)
        engine.run()
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
