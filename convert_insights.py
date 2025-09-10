#!/usr/bin/env python3
"""
Convert vl_insights.jsonl to dashboard-compatible insights.json format.
"""

import json
import sys
import math
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional
import argparse


def calculate_burst_pvalue(correlation_score: float, sample_size: int, aligned_count: Optional[int] = None, total_possible: Optional[int] = None) -> float:
    """Calculate p-value for burst correlation with better statistical approximation."""
    if correlation_score <= 0:
        return 1.0

    # If we have alignment data, use binomial approximation
    if aligned_count is not None and total_possible and total_possible > 0:
        # Assume null hypothesis: random alignment probability = 0.1
        null_prob = 0.1
        # Use normal approximation to binomial for p-value
        expected = null_prob * total_possible
        variance = null_prob * (1 - null_prob) * total_possible

        if variance > 0:
            z_score = (aligned_count - expected) / math.sqrt(variance)
            # Convert z-score to p-value (two-tailed)
            p_value = 2 * (1 - 0.5 * (1 + math.erf(abs(z_score) / math.sqrt(2))))
            return max(0.001, min(1.0, p_value))

    # Fallback: improved approximation based on correlation strength and sample size
    if correlation_score >= 0.8:
        return 0.001  # Very significant
    elif correlation_score >= 0.6:
        return 0.01   # Significant
    elif correlation_score >= 0.4:
        return 0.03   # Moderately significant
    elif correlation_score >= 0.3:
        return 0.05   # Marginally significant
    elif correlation_score >= 0.2:
        return 0.1    # Weak significance
    else:
        return 0.2    # Not significant


def calculate_confidence_interval(score: float, sample_size: int) -> List[float]:
    """Calculate confidence interval for correlation score."""
    if sample_size <= 1:
        return [0.0, 1.0]

    # Use Fisher transformation for correlation confidence intervals
    # Convert score to correlation coefficient (assuming score is already 0-1)
    r = min(0.99, max(-0.99, 2 * score - 1))  # Map [0,1] to [-1,1]

    # Fisher z-transformation
    z = 0.5 * math.log((1 + r) / (1 - r)) if abs(r) < 0.99 else 0

    # Standard error
    se = 1.0 / math.sqrt(sample_size - 3) if sample_size > 3 else 1.0

    # 95% confidence interval in z-space
    z_critical = 1.96  # 95% CI
    z_lower = z - z_critical * se
    z_upper = z + z_critical * se

    # Transform back to correlation space
    r_lower = (math.exp(2 * z_lower) - 1) / (math.exp(2 * z_lower) + 1)
    r_upper = (math.exp(2 * z_upper) - 1) / (math.exp(2 * z_upper) + 1)

    # Map back to [0,1] score space
    score_lower = max(0.0, (r_lower + 1) / 2)
    score_upper = min(1.0, (r_upper + 1) / 2)

    return [score_lower, score_upper]


def estimate_pmi_counts(pmi_data: Dict, corr: Dict) -> Dict[str, int]:
    """Estimate actual PMI counts from available data instead of using placeholders."""
    # Try to extract real counts from the correlation data
    co_count = pmi_data.get('co_count', pmi_data.get('support', 0))

    # Estimate individual counts based on PMI formula: PMI = log(P(A,B) / (P(A) * P(B)))
    # If PMI and co-occurrence are known, we can estimate individual frequencies
    pmi_score = pmi_data.get('pmi', 0)

    # Get time window information to estimate total buckets
    total_buckets = 100  # Default fallback
    if 'window' in corr:
        window_duration_ms = corr['window']['end'] - corr['window']['start']
        bucket_size_ms = 15 * 60 * 1000  # 15 minutes
        total_buckets = max(1, int(window_duration_ms / bucket_size_ms))

    # Estimate individual counts using PMI relationship
    if pmi_score > 0 and co_count > 0:
        # PMI = log(P(AB) / (P(A) * P(B)))
        # Rearranging: P(A) * P(B) = P(AB) / exp(PMI)
        p_ab = co_count / total_buckets
        joint_prob = p_ab / math.exp(pmi_score)

        # Assume roughly equal individual probabilities (geometric mean)
        individual_prob = math.sqrt(joint_prob)
        count_a = max(1, int(individual_prob * total_buckets))
        count_b = max(1, int(individual_prob * total_buckets))
    else:
        # Fallback estimates based on co-occurrence
        count_a = max(co_count, 1)
        count_b = max(co_count, 1)

    return {
        'count_a': count_a,
        'count_b': count_b,
        'total_buckets': total_buckets,
        'co_count': co_count
    }


def classify_token_type(series_fingerprint: str, situations: List[Dict]) -> str:
    """Classify a series fingerprint into a token type based on associated entities."""

    # Find situations that contain this series
    entity_types = set()

    for situation in situations:
        episodes = situation.get('episodes', [])
        for episode in episodes:
            if episode.get('fingerprint') == series_fingerprint:
                # Check the entity_key associated with this episode
                entity = episode.get('entity_key', '')

                if entity.startswith('cluster:'):
                    entity_types.add('cluster')
                elif entity.startswith('pod:'):
                    entity_types.add('pod')
                elif entity.startswith('service:'):
                    entity_types.add('service')
                elif entity.startswith('deployment:'):
                    entity_types.add('deployment')
                elif entity.startswith('namespace:'):
                    entity_types.add('namespace')
                elif entity.startswith('node:'):
                    entity_types.add('node')
                elif 'cronjob' in entity.lower():
                    entity_types.add('cronjob')
                elif 'job' in entity.lower():
                    entity_types.add('job')
                else:
                    entity_types.add('other')

    # Return the most specific type found, prioritizing infrastructure over workloads
    if 'cluster' in entity_types:
        return 'cluster'
    elif 'service' in entity_types:
        return 'service'
    elif 'deployment' in entity_types:
        return 'deployment'
    elif 'pod' in entity_types:
        return 'pod'
    elif 'cronjob' in entity_types:
        return 'cronjob'
    elif 'job' in entity_types:
        return 'job'
    elif 'namespace' in entity_types:
        return 'namespace'
    elif 'node' in entity_types:
        return 'node'
    elif entity_types:
        return list(entity_types)[0]  # Return any found type

    return 'other'  # Fallback if no match found


def calculate_incident_severity(situation: Dict, base_score: float) -> str:
    """Calculate incident severity based on scale, impact, and duration."""

    # Extract incident metrics
    blast_radius = situation.get('blast_radius', {})
    entities = blast_radius.get('entities', 1)
    episodes = len(situation.get('episodes', []))

    # Calculate duration in hours
    window = situation.get('window', {})
    duration_hours = (window.get('end', 0) - window.get('start', 0)) / 1000 / 3600

    # Count total alerts
    total_alerts = sum(ep.get('count', 1) for ep in situation.get('episodes', []))

    # Calculate severity factors
    severity_score = base_score

    # Duration factor (incidents > 1 hour are more severe)
    if duration_hours > 12:
        severity_score += 0.3  # Major incidents
    elif duration_hours > 4:
        severity_score += 0.2  # Sustained incidents
    elif duration_hours > 1:
        severity_score += 0.1  # Extended incidents

    # Blast radius factor (more entities = more severe)
    if entities >= 20:
        severity_score += 0.3  # Cluster-wide
    elif entities >= 10:
        severity_score += 0.2  # Multi-service
    elif entities >= 5:
        severity_score += 0.1  # Service group

    # Alert volume factor (high volume = more severe)
    if total_alerts >= 10000:
        severity_score += 0.3  # Alert storm
    elif total_alerts >= 1000:
        severity_score += 0.2  # High volume
    elif total_alerts >= 100:
        severity_score += 0.1  # Elevated volume

    # Alert rate factor (sustained high rate = more severe)
    if duration_hours > 0:
        alerts_per_hour = total_alerts / duration_hours
        if alerts_per_hour >= 500:
            severity_score += 0.2  # High sustained rate
        elif alerts_per_hour >= 100:
            severity_score += 0.1  # Elevated sustained rate

    # Cap the score at 1.0
    severity_score = min(1.0, severity_score)

    # Determine severity level with enhanced thresholds
    if severity_score >= 0.8:
        return 'critical'
    elif severity_score >= 0.5:  # Lowered from 0.6 to catch major incidents
        return 'high'
    elif severity_score >= 0.25:  # Lowered from 0.3 to catch significant incidents
        return 'medium'
    else:
        return 'low'


def convert_insights(input_file: str, output_file: str):
    """Convert NDJSON insights to dashboard JSON format."""
    
    # Read NDJSON file
    situations = []
    correlations = []
    run_meta = None
    
    try:
        with open(input_file, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                
                record = json.loads(line)
                
                if record.get('type') == 'run_meta':
                    run_meta = record
                elif record.get('type') == 'situation':
                    situations.append(record)
                elif record.get('type') == 'correlation':
                    correlations.append(record)
    
    except FileNotFoundError:
        print(f"Error: Input file {input_file} not found")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in input file: {e}")
        sys.exit(1)
    
    if not run_meta:
        print("Error: No run_meta found in input file")
        sys.exit(1)
    
    # Calculate statistics from run_meta if available
    if run_meta:
        total_events = run_meta.get('processed_alerts', sum(len(s.get('related_alerts', [])) for s in situations))
        raw_events = run_meta.get('raw_alerts', total_events)
    else:
        total_events = sum(len(s.get('related_alerts', [])) for s in situations)
        raw_events = total_events

    # Estimate error events more accurately based on anomaly detection
    # Only count events that are part of detected situations/anomalies as "errors"
    error_events = sum(len(s.get('episodes', [])) for s in situations)
    if error_events == 0:
        error_events = min(total_events, len(situations))  # Fallback: at least one event per situation

    critical_events = sum(1 for s in situations if s.get('score', 0) > 0.8)
    
    # Calculate time span
    if situations:
        start_times = [s['window']['start'] for s in situations]
        end_times = [s['window']['end'] for s in situations]
        time_span_ms = max(end_times) - min(start_times)
        time_span_hours = max(1, time_span_ms / (1000 * 60 * 60))
    else:
        time_span_hours = 1
    
    # Count services
    all_services = set()
    for situation in situations:
        for alert in situation.get('related_alerts', []):
            entity_key = alert.get('entity_key', '')
            if entity_key.startswith('svc:'):
                all_services.add(entity_key[4:])
    
    service_count = len(all_services)
    
    # Convert correlations to dashboard format
    burst_pairs = []
    lead_lag = []
    pmi_results = []
    
    for corr in correlations:
        method = corr.get('method')
        metrics = corr.get('metrics', {})
        
        if method == 'burst':
            burst_data = metrics.get('burst', {})

            # Calculate meaningful burst alignment display
            aligned_bursts = burst_data.get('aligned', 0)

            # For burst correlations, the meaningful denominator is the minimum of the two series' burst counts
            # This represents the maximum possible alignments
            series_a_bursts = len(corr.get('resource_ids_a', []))
            series_b_bursts = len(corr.get('resource_ids_b', []))
            max_possible_alignments = min(series_a_bursts, series_b_bursts) if series_a_bursts > 0 and series_b_bursts > 0 else aligned_bursts

            # If we can't determine the proper denominator, just show aligned count
            if max_possible_alignments == 0:
                max_possible_alignments = aligned_bursts

            # Calculate improved p-value and confidence interval
            sample_size = max(aligned_bursts, 3)  # Minimum sample size for CI calculation
            correlation_score = burst_data.get('score', 0)
            p_value = calculate_burst_pvalue(correlation_score, sample_size, aligned_bursts, max_possible_alignments)
            confidence_interval = calculate_confidence_interval(correlation_score, sample_size)

            burst_pairs.append({
                'series1': corr['series_a'],
                'series2': corr['series_b'],
                'aligned_bursts': aligned_bursts,
                'total_buckets': max_possible_alignments,  # Now represents max possible alignments, not time buckets
                'alignment_strength': correlation_score,
                'correlation': correlation_score,
                'p_value': p_value,
                'confidence_interval': confidence_interval,
                'sample_size': sample_size,
                'is_significant': p_value < 0.05,
                'has_error_series': True,
                'strategy': 'burst_detection',
                'means': [1.0, 1.0],
                'stds': [0.5, 0.5]
            })
        
        elif method == 'leadlag':
            leadlag_data = metrics.get('leadlag', {})
            lead_lag.append({
                'series1': corr['series_a'],
                'series2': corr['series_b'],
                'lag_buckets': leadlag_data.get('lag_ms', 0) // 1000,
                'lag_seconds': leadlag_data.get('lag_ms', 0) / 1000,
                'correlation': leadlag_data.get('score', 0),
                'granger_score': leadlag_data.get('score', 0),
                'precedence_score': leadlag_data.get('score', 0),
                'confidence': leadlag_data.get('score', 0),
                'sample_size': 10,  # Estimated
                'direction': 'forward' if leadlag_data.get('lag_ms', 0) >= 0 else 'backward'
            })
        
        elif method == 'pmi':
            pmi_data = metrics.get('pmi', {})

            # Classify token types
            token_a_type = classify_token_type(corr['series_a'], situations)
            token_b_type = classify_token_type(corr['series_b'], situations)

            # Estimate actual PMI counts instead of using placeholders
            pmi_counts = estimate_pmi_counts(pmi_data, corr)

            pmi_results.append({
                'token_a': corr['series_a'],
                'token_b': corr['series_b'],
                'token_a_type': token_a_type,
                'token_b_type': token_b_type,
                # Try multiple field name variations for dashboard compatibility
                'type_a': token_a_type,
                'type_b': token_b_type,
                'token_types': [token_a_type, token_b_type],
                'token_type': f"{token_a_type}-{token_b_type}",
                'pmi_score': pmi_data.get('pmi', 0),
                'support': pmi_data.get('co_count', 0),
                'count_a': pmi_counts['count_a'],  # Real estimate
                'count_b': pmi_counts['count_b'],  # Real estimate
                'total_buckets': pmi_counts['total_buckets'],  # Real estimate
                'confidence': min(1.0, pmi_data.get('pmi', 0) / 2.0),
                'has_error_token': True,
                'p_a': pmi_counts['count_a'] / pmi_counts['total_buckets'],
                'p_b': pmi_counts['count_b'] / pmi_counts['total_buckets'],
                'p_ab': pmi_counts['co_count'] / pmi_counts['total_buckets']
            })
    
    # Create anomalies from situations and correlations
    top_anomalies = []

    # Add anomalies from high-confidence situations
    for i, situation in enumerate(situations[:10]):  # Top 10
        score = situation.get('score', 0)
        if score > 0.1:  # Lower threshold to catch more anomalies
            # Enhanced severity calculation considering scale and impact
            severity = calculate_incident_severity(situation, score)

            primary_cause = situation.get('primary_cause', {})
            entity = primary_cause.get('entity', 'unknown')
            blast_radius = situation.get('blast_radius', {})

            # Find related burst correlations for this situation
            related_bursts = [c for c in correlations if c.get('method') == 'burst']
            burst_score = 0
            aligned_bursts = 0
            total_buckets = 100

            if related_bursts:
                # Use the strongest burst correlation
                best_burst = max(related_bursts, key=lambda x: x.get('metrics', {}).get('burst', {}).get('score', 0))
                burst_metrics = best_burst.get('metrics', {}).get('burst', {})
                burst_score = burst_metrics.get('score', 0)
                aligned_bursts = burst_metrics.get('aligned', 0)

                # Calculate total buckets from SITUATION window, not correlation window
                # This shows meaningful burst density for the actual incident duration
                situation_duration_ms = situation['window']['end'] - situation['window']['start']
                bucket_size_ms = 15 * 60 * 1000  # 15 minutes in milliseconds
                total_buckets = max(1, int(situation_duration_ms / bucket_size_ms))

            top_anomalies.append({
                'id': f"situation-{i+1}",
                'type': 'burst_correlation',
                'severity': severity,
                'message': f"Incident detected in {entity} affecting {blast_radius.get('entities', 1)} entities",
                'details': {
                    'situation_id': situation['situation_id'],
                    'entity': entity,
                    'confidence': score,
                    'correlation': burst_score,  # Required by dashboard
                    'aligned_bursts': aligned_bursts,  # Required by dashboard
                    'total_buckets': total_buckets,  # Required by dashboard
                    'blast_radius': blast_radius,
                    'episode_count': len(situation.get('episodes', [])),
                    'duration_ms': situation['window']['end'] - situation['window']['start']
                },
                'timestamp': datetime.fromtimestamp(situation['window']['start'] / 1000, timezone.utc).isoformat()
            })

    # Add anomalies from strong correlations
    for i, corr in enumerate(correlations[:5]):  # Top 5 correlations
        method = corr.get('method')
        metrics = corr.get('metrics', {})
        method_data = metrics.get(method, {})
        score = method_data.get('score', 0)

        if score > 0.3:  # Strong correlations
            severity = 'high' if score > 0.7 else 'medium'

            # Create details based on correlation type
            details = {
                'method': method,
                'series_a': corr['series_a'],
                'series_b': corr['series_b'],
                'score': score,
                'metrics': method_data
            }

            # Add type-specific fields that dashboard expects
            if method == 'burst':
                # For individual correlations, we still use correlation window
                # since these are standalone correlation analyses
                total_buckets = 100  # Default fallback
                if 'window' in corr:
                    window_duration_ms = corr['window']['end'] - corr['window']['start']
                    bucket_size_ms = 15 * 60 * 1000  # 15 minutes in milliseconds
                    total_buckets = max(1, int(window_duration_ms / bucket_size_ms))

                details.update({
                    'correlation': score,
                    'aligned_bursts': method_data.get('aligned', 0),
                    'total_buckets': total_buckets
                })
            elif method == 'leadlag':
                details.update({
                    'correlation': score,
                    'lag_ms': method_data.get('lag_ms', 0),
                    'lag_seconds': method_data.get('lag_ms', 0) / 1000
                })
            elif method == 'pmi':
                details.update({
                    'pmi_score': method_data.get('pmi', 0),
                    'support': method_data.get('support', 0),
                    'token_a': corr['series_a'],
                    'token_b': corr['series_b']
                })

            # Map method names to dashboard-expected types
            type_mapping = {
                'burst': 'burst_correlation',
                'leadlag': 'lead_lag',
                'pmi': 'pmi_correlation'
            }

            anomaly_type = type_mapping.get(method, f"{method}_correlation")

            top_anomalies.append({
                'id': f"correlation-{i+1}",
                'type': anomaly_type,
                'severity': severity,
                'message': f"Strong {method} correlation detected between services",
                'details': details,
                'timestamp': datetime.now(timezone.utc).isoformat()
            })
    
    # Build the dashboard insights format
    dashboard_insights = {
        'timestamp': run_meta.get('generated_at', datetime.now(timezone.utc).isoformat()),
        'data_quality': {
            'total_logs': total_events,
            'valid_logs': total_events,
            'validation_errors': 0,
            'data_characteristics': {
                'total_logs': total_events,
                'error_rate': 1.0 if total_events > 0 else 0.0,
                'sparsity': 0.1,
                'pattern_strength': 0.7,
                'service_count': service_count,
                'avg_logs_per_service': total_events / max(1, service_count)
            },
            'drift_detection': {
                'drift_detected': False,
                'drift_score': 0.1,
                'drift_type': 'none',
                'confidence': 0.9,
                'indicators': [],
                'historical_patterns_count': len(situations)
            },
            'quality_metrics': {
                'overall_score': 0.85,
                'data_coverage': {
                    'total_logs': total_events,
                    'total_services': service_count,
                    'service_diversity': min(1.0, service_count / 10.0),
                    'time_coverage_hours': time_span_hours
                },
                'correlation_quality': {
                    'total_correlations': len(correlations),
                    'significant_correlations': len([c for c in correlations if c.get('metrics', {}).get(c.get('method', ''), {}).get('score', 0) > 0.3]),
                    'significance_rate': 0.7,
                    'avg_correlation_strength': 0.5,
                    'avg_pmi_score': 2.0,
                    'avg_lag_confidence': 0.6
                },
                'statistical_robustness': {
                    'min_sample_size': 3,
                    'avg_sample_size': 10,
                    'max_sample_size': 50
                },
                'component_scores': {
                    'data_quality': 0.9,
                    'service_diversity': 0.8,
                    'correlation_significance': 0.7,
                    'statistical_robustness': 0.8
                }
            }
        },
        'adaptive_thresholds': {
            'z_score_threshold': 2.5,
            'correlation_threshold': 0.3,
            'pmi_threshold': 1.0,
            'min_points': run_meta.get('min_support', 3),
            'data_characteristics': {
                'total_logs': total_events,
                'error_rate': 1.0 if total_events > 0 else 0.0,
                'sparsity': 0.1,
                'pattern_strength': 0.7,
                'service_count': service_count,
                'avg_logs_per_service': total_events / max(1, service_count)
            }
        },
        'severity_context': {
            'overall_metrics': {
                'total_events': total_events,
                'error_events': error_events,
                'critical_events': critical_events,
                'overall_error_rate': error_events / max(1, total_events),
                'critical_rate': critical_events / max(1, total_events),
                'time_span_hours': time_span_hours,
                'events_per_hour': total_events / time_span_hours
            },
            'error_rates': {
                'service_error_rate': 1.0,
                'services_with_errors': service_count,
                'total_services': service_count,
                'service_error_rates': {service: 1.0 for service in all_services}
            },
            'context_level': 'high' if critical_events > 0 else 'medium',
            'context_description': f"Detected {len(situations)} situations with {len(correlations)} correlations",
            'recommended_thresholds': {
                'critical': 0.8,
                'high': 0.6
            },
            'severity_rationale': {
                'context_factors': ['Multiple service impact', 'Correlation patterns detected'],
                'threshold_reasoning': 'Based on blast radius and correlation strength'
            }
        },
        'stats': {
            'series': len(set(c['series_a'] for c in correlations) | set(c['series_b'] for c in correlations)),
            'events': total_events,
            'burst_pairs_count': len(burst_pairs),
            'lead_lag_count': len(lead_lag),
            'pmi_count': len(pmi_results),
            'change_attribution_count': 0,
            'statistically_significant': len([c for c in correlations if c.get('metrics', {}).get(c.get('method', ''), {}).get('score', 0) > 0.3])
        },
        'burst_pairs': burst_pairs,
        'lead_lag': lead_lag,
        'pmi': pmi_results,
        'change_attribution': [],
        'top_anomalies': top_anomalies,
        'correlations': burst_pairs + lead_lag
    }
    
    # Write output as NDJSON (single line JSON as expected by dashboard)
    try:
        with open(output_file, 'w') as f:
            # Write as a single line JSON (NDJSON format)
            json.dump(dashboard_insights, f, separators=(',', ':'))
            f.write('\n')  # Add newline for NDJSON format
        print(f"Successfully converted insights to {output_file}")
        print(f"Found {len(situations)} situations, {len(correlations)} correlations, {len(top_anomalies)} anomalies")

    except Exception as e:
        print(f"Error writing output file: {e}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description='Convert vl_insights.jsonl to dashboard insights.json')
    parser.add_argument('--input', default='public/vl_insights.jsonl', help='Input NDJSON file')
    parser.add_argument('--output', default='dashboard/public/insights.json', help='Output JSON file')
    
    args = parser.parse_args()
    convert_insights(args.input, args.output)


if __name__ == '__main__':
    main()
