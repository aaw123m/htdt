"""Informed joint refinement, retaining both preceding failed studies."""
import run_r130d_finite_band_grid_continuation as driver

driver.PLAN_SHA = '6230455eec3aa43dea6991119cda67ea275c04b3a74705a6bef680b2e4a187bc'
driver.PLAN_FILE = 'r130d_finite_band_grid_refined_plan_2026-10-10.json'
driver.EVIDENCE_FILE = 'r130d_finite_band_grid_refined_evidence_2026-10-10.json'

if __name__ == '__main__':
    driver.main()
