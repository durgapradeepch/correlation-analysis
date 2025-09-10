import React, { useState } from "react";
import { Insights } from "../types/insights";
import {
  AlertTriangle,
  TrendingUp,
  Activity,
  Database,
  BarChart3,
  PieChart,
  AlertCircle,
  CheckCircle,
  XCircle,
  Info,
  Zap,
  Target,
  Filter,
  RefreshCw,
} from "lucide-react";

interface ErrorAnalysisProps {
  insights: Insights;
}

const ErrorAnalysis: React.FC<ErrorAnalysisProps> = ({ insights }) => {
  const [selectedTimeRange, setSelectedTimeRange] = useState<string>("24h");
  const [selectedSeverity, setSelectedSeverity] = useState<string>("all");

  const getSeverityColor = (severity: string) => {
    switch (severity) {
      case "critical":
        return "bg-error-100 text-error-800 border-error-200";
      case "high":
        return "bg-warning-100 text-warning-800 border-warning-200";
      case "medium":
        return "bg-info-100 text-info-800 border-info-200";
      case "low":
        return "bg-success-100 text-success-800 border-success-200";
      default:
        return "bg-neutral-100 text-neutral-800 border-neutral-200";
    }
  };

  const getSeverityIcon = (severity: string) => {
    switch (severity) {
      case "critical":
        return <XCircle className="w-4 h-4" />;
      case "high":
        return <AlertTriangle className="w-4 h-4" />;
      case "medium":
        return <AlertCircle className="w-4 h-4" />;
      case "low":
        return <Info className="w-4 h-4" />;
      default:
        return <CheckCircle className="w-4 h-4" />;
    }
  };

  const formatNumber = (num: number, decimals: number = 0) => {
    return new Intl.NumberFormat("en-US", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    }).format(num);
  };

  const formatPercentage = (num: number) => {
    return `${(num * 100).toFixed(1)}%`;
  };

  // Calculate error metrics
  // Calculate error metrics - fix the interpretation
  const totalProcessedEvents =
    insights.severity_context.overall_metrics.total_events;
  const totalRawAlerts = 16872; // From the raw alerts.json file
  const totalAnomalies = insights.top_anomalies.length; // Total: 13 anomalies

  // Count actual errors from anomalies (critical, high, medium are errors)
  const criticalAnomalies = insights.top_anomalies.filter(
    (a) => a.severity === "critical"
  ).length;
  const highAnomalies = insights.top_anomalies.filter(
    (a) => a.severity === "high"
  ).length;
  const mediumAnomalies = insights.top_anomalies.filter(
    (a) => a.severity === "medium"
  ).length;
  const lowAnomalies = insights.top_anomalies.filter(
    (a) => a.severity === "low"
  ).length;
  const totalErrorAnomalies =
    criticalAnomalies + highAnomalies + mediumAnomalies;

  const errorMetrics = {
    totalAnomalies: totalAnomalies, // All 13 anomalies
    totalErrors: totalErrorAnomalies, // Error anomalies (critical + high + medium): 5
    criticalErrors: criticalAnomalies, // Critical anomalies only: 1
    lowSeverityAnomalies: lowAnomalies, // Low severity anomalies: 8
    errorRate: totalErrorAnomalies / totalProcessedEvents, // Error rate from processed events
    criticalRate: criticalAnomalies / totalProcessedEvents, // Critical rate from processed events
    anomalyRate: totalAnomalies / totalProcessedEvents, // Total anomaly detection rate
    servicesWithErrors:
      insights.severity_context.error_rates.services_with_errors || 0,
    totalServices: insights.severity_context.error_rates.total_services || 0,
    serviceErrorRate:
      insights.severity_context.error_rates.service_error_rate || 0,
    eventsPerHour: insights.severity_context.overall_metrics.events_per_hour,
    timeSpanHours: insights.severity_context.overall_metrics.time_span_hours,
    totalRawAlerts: totalRawAlerts,
    totalProcessedEvents: totalProcessedEvents,
    processingEfficiency: totalProcessedEvents / totalRawAlerts,
  };

  // Group anomalies by severity
  const anomaliesBySeverity = insights.top_anomalies.reduce((acc, anomaly) => {
    acc[anomaly.severity] = (acc[anomaly.severity] || 0) + 1;
    return acc;
  }, {} as Record<string, number>);

  // Calculate error trends (mock data for demonstration)
  const errorTrends = {
    hourlyErrors: Array.from({ length: 24 }, (_, i) => ({
      hour: i,
      errors: Math.floor(Math.random() * errorMetrics.eventsPerHour * 1.5),
      critical: Math.floor(Math.random() * errorMetrics.eventsPerHour * 0.3),
    })),
    dailyTrend: errorMetrics.eventsPerHour > 100 ? "increasing" : "stable",
  };

  return (
    <div className="space-y-6">
      {/* Header with Controls */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-neutral-900">
            Error Analysis
          </h2>
          <p className="text-neutral-600 mt-1">
            Comprehensive error tracking and incident analysis
          </p>
        </div>
        <div className="flex items-center gap-4">
          <select
            value={selectedTimeRange}
            onChange={(e) => setSelectedTimeRange(e.target.value)}
            className="px-4 py-2 border border-neutral-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-primary-500"
          >
            <option value="1h">Last Hour</option>
            <option value="24h">Last 24 Hours</option>
            <option value="7d">Last 7 Days</option>
            <option value="30d">Last 30 Days</option>
          </select>
          <select
            value={selectedSeverity}
            onChange={(e) => setSelectedSeverity(e.target.value)}
            className="px-4 py-2 border border-neutral-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-primary-500"
          >
            <option value="all">All Severities</option>
            <option value="critical">Critical Only</option>
            <option value="high">High & Above</option>
            <option value="medium">Medium & Above</option>
          </select>
          <button className="btn btn-secondary">
            <RefreshCw className="w-4 h-4 mr-2" />
            Refresh
          </button>
        </div>
      </div>

      {/* Error Overview Cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-6">
        <div className="metric-card">
          <div className="metric-header">
            <div className="metric-icon error">
              <AlertTriangle className="w-6 h-6" />
            </div>
            <div className="metric-title">Total Anomalies</div>
          </div>
          <div className="metric-content">
            <div className="metric-value">
              {formatNumber(errorMetrics.totalAnomalies)}
            </div>
            <div className="metric-subtitle">
              {formatPercentage(errorMetrics.anomalyRate)} detection rate
            </div>
          </div>
        </div>

        <div className="metric-card">
          <div className="metric-header">
            <div className="metric-icon critical">
              <XCircle className="w-6 h-6" />
            </div>
            <div className="metric-title">Critical Issues</div>
          </div>
          <div className="metric-content">
            <div className="metric-value">
              {formatNumber(errorMetrics.criticalErrors)}
            </div>
            <div className="metric-subtitle">
              {errorMetrics.criticalErrors > 0
                ? formatPercentage(errorMetrics.criticalRate)
                : "0.0%"}{" "}
              critical rate
            </div>
          </div>
        </div>

        <div className="metric-card">
          <div className="metric-header">
            <div className="metric-icon warning">
              <Database className="w-6 h-6" />
            </div>
            <div className="metric-title">Alert Processing</div>
          </div>
          <div className="metric-content">
            <div className="metric-value">
              {formatNumber(errorMetrics.totalProcessedEvents)}/
              {formatNumber(errorMetrics.totalRawAlerts)}
            </div>
            <div className="metric-subtitle">
              {formatPercentage(errorMetrics.processingEfficiency)} processed
            </div>
          </div>
        </div>

        <div className="metric-card">
          <div className="metric-header">
            <div className="metric-icon info">
              <Activity className="w-6 h-6" />
            </div>
            <div className="metric-title">Alert Velocity</div>
          </div>
          <div className="metric-content">
            <div className="metric-value">
              {formatNumber(
                errorMetrics.totalRawAlerts / errorMetrics.timeSpanHours,
                0
              )}
            </div>
            <div className="metric-subtitle">alerts per hour</div>
          </div>
        </div>
      </div>

      {/* Error Severity Distribution */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="card">
          <div className="card-header">
            <div className="flex items-center gap-3">
              <PieChart className="w-5 h-5 text-neutral-600" />
              <div>
                <h3 className="card-title">Severity Distribution</h3>
                <p className="card-subtitle">Breakdown of error severities</p>
              </div>
            </div>
          </div>
          <div className="card-content">
            <div className="space-y-4">
              {Object.entries(anomaliesBySeverity).map(([severity, count]) => (
                <div
                  key={severity}
                  className="flex items-center justify-between"
                >
                  <div className="flex items-center gap-3">
                    <div
                      className={`px-3 py-1 rounded-full text-sm font-medium border ${getSeverityColor(
                        severity
                      )}`}
                    >
                      <div className="flex items-center gap-2">
                        {getSeverityIcon(severity)}
                        {severity.charAt(0).toUpperCase() + severity.slice(1)}
                      </div>
                    </div>
                  </div>
                  <div className="text-right">
                    <div className="font-semibold text-neutral-900">
                      {count}
                    </div>
                    <div className="text-sm text-neutral-500">
                      {((count / insights.top_anomalies.length) * 100).toFixed(
                        1
                      )}
                      %
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>

        <div className="card">
          <div className="card-header">
            <div className="flex items-center gap-3">
              <BarChart3 className="w-5 h-5 text-neutral-600" />
              <div>
                <h3 className="card-title">Error Trends</h3>
                <p className="card-subtitle">24-hour error pattern analysis</p>
              </div>
            </div>
          </div>
          <div className="card-content">
            <div className="space-y-4">
              <div className="flex items-center justify-between p-4 bg-neutral-50 rounded-lg">
                <div className="flex items-center gap-3">
                  <TrendingUp className="w-5 h-5 text-primary-600" />
                  <div>
                    <div className="font-medium text-neutral-900">
                      Trend Analysis
                    </div>
                    <div className="text-sm text-neutral-600">
                      Error pattern over {errorMetrics.timeSpanHours.toFixed(1)}{" "}
                      hours
                    </div>
                  </div>
                </div>
                <div
                  className={`px-3 py-1 rounded-full text-sm font-medium ${
                    errorTrends.dailyTrend === "increasing"
                      ? "bg-error-100 text-error-800"
                      : "bg-success-100 text-success-800"
                  }`}
                >
                  {errorTrends.dailyTrend === "increasing"
                    ? "Increasing"
                    : "Stable"}
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div className="text-center p-3 bg-primary-50 rounded-lg">
                  <div className="text-2xl font-bold text-primary-600">
                    {formatNumber(errorMetrics.eventsPerHour)}
                  </div>
                  <div className="text-sm text-primary-700">Avg/Hour</div>
                </div>
                <div className="text-center p-3 bg-warning-50 rounded-lg">
                  <div className="text-2xl font-bold text-warning-600">
                    {formatNumber(
                      errorMetrics.criticalErrors / errorMetrics.timeSpanHours,
                      1
                    )}
                  </div>
                  <div className="text-sm text-warning-700">Critical/Hour</div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Service Error Analysis */}
      <div className="card">
        <div className="card-header">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <Database className="w-5 h-5 text-neutral-600" />
              <div>
                <h3 className="card-title">Service Error Analysis</h3>
                <p className="card-subtitle">
                  Error rates by service and component
                </p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <button className="btn btn-ghost btn-sm">
                <Filter className="w-4 h-4 mr-2" />
                Filter
              </button>
            </div>
          </div>
        </div>
        <div className="card-content">
          <div className="space-y-4">
            {Object.entries(
              insights.severity_context.error_rates.service_error_rates || {}
            )
              .slice(0, 10)
              .map(([service, rate]) => (
                <div
                  key={service}
                  className="flex items-center justify-between p-4 bg-neutral-50 rounded-lg hover:bg-neutral-100 transition-colors"
                >
                  <div className="flex items-center gap-3">
                    <div
                      className={`w-3 h-3 rounded-full ${
                        rate > 0.5
                          ? "bg-error-500"
                          : rate > 0.2
                          ? "bg-warning-500"
                          : rate > 0.1
                          ? "bg-info-500"
                          : "bg-success-500"
                      }`}
                    ></div>
                    <div>
                      <div className="font-medium text-neutral-900">
                        {service}
                      </div>
                      <div className="text-sm text-neutral-600">
                        Service component
                      </div>
                    </div>
                  </div>
                  <div className="text-right">
                    <div className="font-semibold text-neutral-900">
                      {formatPercentage(rate)}
                    </div>
                    <div className="text-sm text-neutral-500">error rate</div>
                  </div>
                </div>
              ))}

            {Object.keys(
              insights.severity_context.error_rates.service_error_rates || {}
            ).length === 0 && (
              <div className="text-center py-8 text-neutral-500">
                <Database className="w-12 h-12 mx-auto mb-4 opacity-50" />
                <p>No service-specific error data available</p>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Recent Critical Incidents */}
      <div className="card">
        <div className="card-header">
          <div className="flex items-center gap-3">
            <Zap className="w-5 h-5 text-neutral-600" />
            <div>
              <h3 className="card-title">Recent Critical Incidents</h3>
              <p className="card-subtitle">
                High-priority anomalies requiring attention
              </p>
            </div>
          </div>
        </div>
        <div className="card-content">
          <div className="space-y-4">
            {insights.top_anomalies
              .filter(
                (anomaly) =>
                  selectedSeverity === "all" ||
                  (selectedSeverity === "critical" &&
                    anomaly.severity === "critical") ||
                  (selectedSeverity === "high" &&
                    ["critical", "high"].includes(anomaly.severity)) ||
                  (selectedSeverity === "medium" &&
                    ["critical", "high", "medium"].includes(anomaly.severity))
              )
              .slice(0, 5)
              .map((anomaly, index) => (
                <div
                  key={anomaly.id}
                  className="p-4 border border-neutral-200 rounded-lg hover:shadow-md transition-shadow"
                >
                  <div className="flex items-start justify-between">
                    <div className="flex items-start gap-3">
                      <div
                        className={`w-10 h-10 rounded-lg flex items-center justify-center ${
                          anomaly.severity === "critical"
                            ? "bg-error-100"
                            : anomaly.severity === "high"
                            ? "bg-warning-100"
                            : anomaly.severity === "medium"
                            ? "bg-info-100"
                            : "bg-success-100"
                        }`}
                      >
                        {getSeverityIcon(anomaly.severity)}
                      </div>
                      <div className="flex-1">
                        <div className="flex items-center gap-2 mb-2">
                          <span
                            className={`px-2 py-1 rounded-full text-xs font-medium ${getSeverityColor(
                              anomaly.severity
                            )}`}
                          >
                            {anomaly.severity.toUpperCase()}
                          </span>
                          <span className="text-xs text-neutral-500">
                            {new Date(anomaly.timestamp).toLocaleString()}
                          </span>
                        </div>
                        <h4 className="font-medium text-neutral-900 mb-1">
                          {anomaly.message}
                        </h4>
                        <div className="text-sm text-neutral-600">
                          Type: {anomaly.type.replace("_", " ").toUpperCase()}
                        </div>
                        {anomaly.details && (
                          <div className="mt-2 text-sm text-neutral-600">
                            {anomaly.details.correlation && (
                              <span>
                                Correlation:{" "}
                                {(anomaly.details.correlation * 100).toFixed(1)}
                                %
                              </span>
                            )}
                            {anomaly.details.confidence && (
                              <span className="ml-4">
                                Confidence:{" "}
                                {(anomaly.details.confidence * 100).toFixed(1)}%
                              </span>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                    <button className="btn btn-ghost btn-sm">
                      <Target className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              ))}

            {insights.top_anomalies.length === 0 && (
              <div className="text-center py-8 text-neutral-500">
                <CheckCircle className="w-12 h-12 mx-auto mb-4 text-success-500" />
                <p>No critical incidents detected</p>
                <p className="text-sm mt-1">System is operating normally</p>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Error Analysis Summary */}
      <div className="card">
        <div className="card-header">
          <div className="flex items-center gap-3">
            <BarChart3 className="w-5 h-5 text-neutral-600" />
            <div>
              <h3 className="card-title">Analysis Summary</h3>
              <p className="card-subtitle">Key insights and recommendations</p>
            </div>
          </div>
        </div>
        <div className="card-content">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div className="space-y-4">
              <h4 className="font-semibold text-neutral-900">Key Findings</h4>
              <div className="space-y-3">
                <div className="flex items-start gap-3">
                  <div className="w-2 h-2 bg-primary-500 rounded-full mt-2"></div>
                  <div className="text-sm text-neutral-700">
                    <strong>{formatNumber(errorMetrics.totalRawAlerts)}</strong>{" "}
                    raw alerts processed into{" "}
                    <strong>{errorMetrics.totalProcessedEvents}</strong> events
                    with <strong>{errorMetrics.totalAnomalies}</strong> total
                    anomalies (<strong>{errorMetrics.totalErrors}</strong>{" "}
                    errors, <strong>{errorMetrics.lowSeverityAnomalies}</strong>{" "}
                    low severity)
                  </div>
                </div>
                <div className="flex items-start gap-3">
                  <div className="w-2 h-2 bg-warning-500 rounded-full mt-2"></div>
                  <div className="text-sm text-neutral-700">
                    Anomaly detection rate of{" "}
                    <strong>{formatPercentage(errorMetrics.errorRate)}</strong>{" "}
                    with <strong>{errorMetrics.criticalErrors}</strong> critical
                    incidents identified
                  </div>
                </div>
                <div className="flex items-start gap-3">
                  <div className="w-2 h-2 bg-info-500 rounded-full mt-2"></div>
                  <div className="text-sm text-neutral-700">
                    Alert velocity of{" "}
                    <strong>
                      {formatNumber(
                        errorMetrics.totalRawAlerts /
                          errorMetrics.timeSpanHours,
                        0
                      )}
                    </strong>{" "}
                    alerts per hour over{" "}
                    <strong>{errorMetrics.timeSpanHours.toFixed(1)}</strong>{" "}
                    hours
                  </div>
                </div>
              </div>
            </div>

            <div className="space-y-4">
              <h4 className="font-semibold text-neutral-900">
                Recommendations
              </h4>
              <div className="space-y-3">
                <div className="flex items-start gap-3">
                  <CheckCircle className="w-4 h-4 text-success-500 mt-0.5" />
                  <div className="text-sm text-neutral-700">
                    Monitor services with error rates above{" "}
                    {formatPercentage(0.1)}
                  </div>
                </div>
                <div className="flex items-start gap-3">
                  <AlertTriangle className="w-4 h-4 text-warning-500 mt-0.5" />
                  <div className="text-sm text-neutral-700">
                    Investigate critical incidents for root cause analysis
                  </div>
                </div>
                <div className="flex items-start gap-3">
                  <Info className="w-4 h-4 text-info-500 mt-0.5" />
                  <div className="text-sm text-neutral-700">
                    Set up automated alerts for error rate thresholds
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default ErrorAnalysis;
