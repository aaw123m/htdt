import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { ComparisonReportPanel } from './ComparisonReports'
import { PanelErrorBoundary } from './ErrorBoundary'
import { MeasurementSessionsPanel } from './MeasurementSessions'
import { PlacementOverviewPanel } from './PlacementOverview'
import { RewReadonlyPanel } from './RewReadonly'
import './styles.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <PanelErrorBoundary name="Main"><App /></PanelErrorBoundary>
    <PanelErrorBoundary name="Measurement Sessions"><MeasurementSessionsPanel /></PanelErrorBoundary>
    <PanelErrorBoundary name="Saved Comparisons"><ComparisonReportPanel /></PanelErrorBoundary>
    <PanelErrorBoundary name="Layout History"><PlacementOverviewPanel /></PanelErrorBoundary>
    <PanelErrorBoundary name="REW"><RewReadonlyPanel /></PanelErrorBoundary>
  </StrictMode>,
)
