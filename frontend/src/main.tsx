import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { ComparisonReportPanel } from './ComparisonReports'
import { MeasurementSessionsPanel } from './MeasurementSessions'
import { PlacementOverviewPanel } from './PlacementOverview'
import { RewReadonlyPanel } from './RewReadonly'
import './styles.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
    <MeasurementSessionsPanel />
    <ComparisonReportPanel />
    <PlacementOverviewPanel />
    <RewReadonlyPanel />
  </StrictMode>,
)
