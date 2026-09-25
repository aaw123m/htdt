# External Asset Admission Ledger

Cross-domain admission ledger for every external dataset, standard, format,
tool and manufacturer source researched for HTDT (issues #771, #772, #779,
#790–#793, #806–#809, #816).

This document is planning/governance only. It answers:

> What can HTDT legally and technically investigate/import **now**, what
> still needs license/permission review, what should remain
> user-import-only, and what must never be bundled?

**No external payload may be downloaded into the repository merely because
it appears in this ledger.** "Downloadable" is never a synonym for
"redistributable".

## 1. Admission states

One cross-domain vocabulary; every researched asset carries exactly one
current state.

| State | Meaning |
|---|---|
| `READY_FOR_ADMISSION_REVIEW` | License/source is clear enough to perform a bounded technical admission review. Does **not** mean approved for bundling. |
| `BUNDLE_CANDIDATE` | License/provenance appears compatible with distributing a bounded fixture/data package, pending exact file/hash/attribution review. |
| `USER_IMPORT_CANDIDATE` | Format/data are useful but the user supplies the asset; HTDT does not redistribute it. |
| `DOWNLOAD_ON_DEMAND_CANDIDATE` | Open/licensed but too large or too volatile for the base repository/install. |
| `LINK_ONLY` | Useful source/reference; no data redistribution planned. |
| `LOCAL_RESEARCH_ONLY` | Terms/non-commercial restrictions/gated access make it unsuitable for normal distributable fixtures. |
| `PERMISSION_REQUIRED` | Technically valuable; explicit author/vendor/lab permission needed for extraction or redistribution. |
| `LICENSE_REVIEW_REQUIRED` | Current rights state is missing or ambiguous. |
| `STANDARDS_REFERENCE_ONLY` | Paid/copyrighted/licensed standard used for source/version/method semantics. Never bundle the document payload. |
| `SOFTWARE_REUSE_BAKEOFF` | Open-source software/dependency candidate. Code license and binary/runtime/sample-data terms are reviewed independently. |
| `REJECT_FOR_BUNDLING` | Known bad fit for redistribution/product inclusion. |
| `USER_MEASURED` | Evidence produced by the user's own instrument/process on their own equipment — strong provenance for that unit, never generic library truth. |
| `PUBLIC_DOMAIN_COMMUNITY` | Community-contributed corpus published as public domain; admissible for a specific setup only, never auto-applied. |
| `DOCUMENTED_TEXT_INTERCHANGE` | A published, documented text format usable as an interchange contract without reusing the tool's code. |
| `USER_SIDE_LICENSED_IMPORT` | The user imports the asset under their own license/account; HTDT stores user-local evidence but ships nothing. |
| `THIRD_PARTY_DISCOVERY_ONLY` | Third-party database/site used to find models/sources — canonical values must come from primary evidence; no bulk scrape or re-hosting. |

## 2. Acoustic materials ledger

| Asset | Current state | License/rights found | Proposed use |
|---|---|---|---|
| FOAM 01 | BUNDLE_CANDIDATE | Apache-2.0 | normal-incidence absorption/import/#790 validation |
| FOAM 02 | BUNDLE_CANDIDATE | Apache-2.0 | JCA/JCAL/model validation/inverse examples |
| Acoustic Index | LICENSE_REVIEW_REQUIRED | live DB/API; terms/cache/redistribution need review | practical ISO 354 material/product source |
| Zenodo scattering DB 17660089/17660108 | PERMISSION_REQUIRED | current Rights = Copyright | measured scattering/diffusion research |
| 2026 impedance-tube variability 19347719 | LICENSE_REVIEW_REQUIRED | no clear permissive license captured | variability/model research |
| RPG comparison CSV | USER_IMPORT_CANDIDATE | public CSV; redistribution terms not yet reviewed | commercial treatment absorption/diffusion/LF |
| GIK lab reports | LINK_ONLY / PERMISSION_REQUIRED | report reproduction restrictions visible | commercial treatment lab evidence |

## 3. Loudspeaker / source directivity ledger

| Asset | State | License | Proposed use |
|---|---|---|---|
| Aalto 4-source directivity 10255554 | BUNDLE_CANDIDATE | CC BY 4.0 | first SOFA directivity fixture |
| Princeton 3D3A survey | BUNDLE_CANDIDATE | CC BY 4.0 | raw IR/CSV directivity fixture |
| CLF | READY_FOR_ADMISSION_REVIEW | specification/authoring terms still need exact capture | primary loudspeaker interchange |
| SOFA FreeFieldDirectivityTF | READY_FOR_ADMISSION_REVIEW | SOFA ecosystem; dataset rights separate | complex directivity interchange |
| Martin Audio CLF/GLL downloads | USER_IMPORT_CANDIDATE | manufacturer-hosted; redistribution not assumed | real manufacturer CLF test |
| Spinorama | LICENSE_REVIEW_REQUIRED per source | aggregated providers | discovery/user import, no bulk copy |
| Genelec 8030C Zenodo 21486919 | LICENSE_REVIEW_REQUIRED | downloadable but no permissive license captured | SOFA research |
| DirPat | LICENSE_REVIEW_REQUIRED per dataset | per-source review | source/mic directivity research |

## 4. Validation corpus ledger

| Dataset | State | License | Role |
|---|---|---|---|
| FLAIR | BUNDLE_CANDIDATE / DOWNLOAD_ON_DEMAND | CC BY 4.0 | geometry+RIR validation |
| MeshRIR | DOWNLOAD_ON_DEMAND_CANDIDATE | CC BY 4.0 | spatial field/grid validation |
| BUT ReverbDB | DOWNLOAD_ON_DEMAND_CANDIDATE | CC BY 4.0 | multi-room RIR diversity |
| Arni | DOWNLOAD_ON_DEMAND_CANDIDATE | CC BY 4.0 | variable-treatment/repeatability |
| dEchorate | LICENSE_REVIEW_REQUIRED before bundle | repo/data rights must be captured separately | early reflection/source localization |
| BRAS | LICENSE_REVIEW_REQUIRED at exact asset endpoint | benchmark paper says public/free database; exact payload terms to capture | solver benchmark |
| Motus | LICENSE_REVIEW_REQUIRED upstream | public index says CC BY 4.0; upstream check required | furniture/spatial variation |
| Real Acoustic Fields | LOCAL_RESEARCH_ONLY | CC BY-NC 4.0 | dense real/6DoF research |
| AcousticRooms | DOWNLOAD_ON_DEMAND_CANDIDATE | CC BY 4.0 | synthetic stress only, not physical validation |
| RAVes | LICENSE_REVIEW_REQUIRED | Rights field unresolved | SRIR/BRIR/MIMO research |
| High-resolution SRIR 10450779 | LICENSE_REVIEW_REQUIRED | exact upstream license to capture | spatial RIR |
| Niels Bohr Auditorium RIR 14212820 | LICENSE_REVIEW_REQUIRED | Rights unresolved | room-RIR research |

## 5. HRTF / binaural ledger

| Asset | State | Rights | Role |
|---|---|---|---|
| SADIE II | BUNDLE_CANDIDATE | Apache-2.0 stated by York | first mannequin/generic SOFA fixture |
| CIPIC | READY_FOR_ADMISSION_REVIEW | UC permission + attribution; exact converted file provenance to confirm | broad human HRTF |
| ARI | LICENSE_REVIEW_REQUIRED | exact current file terms needed | large subject set |
| HUTUBS | LICENSE_REVIEW_REQUIRED | exact asset terms needed | HRTF/anthropometry/headphone data |
| SOFA repository | LINK_ONLY/DISCOVERY | dataset-specific rights vary | discovery, not blanket license |

## 6. Auralization programme-audio ledger

| Asset | State | License | Role |
|---|---|---|---|
| TU Berlin Beethoven 8 anechoic | DOWNLOAD_ON_DEMAND / BUNDLE_SUBSET_CANDIDATE | CC BY 4.0 | high-quality dry orchestral reference |
| OpenAIR anechoic examples | READY_FOR_ADMISSION_REVIEW | representative pages CC BY 4.0; exact file-level check | small voice/drum fixture |
| AIRCADE | READY_FOR_ADMISSION_REVIEW | stated CC BY 4.0 | source/IR/auralized triplets |
| EBU SQAM | LINK_ONLY / USER_OWNED | track rights mixed/commercial excerpts | external evaluation material |

## 7. Geometry datasets ledger

| Dataset | State | Rights | Role |
|---|---|---|---|
| HTDT-generated defect fixtures | BUNDLE_CANDIDATE | HTDT-owned | canonical CI repair truth |
| FLAIR point cloud | DOWNLOAD_ON_DEMAND_CANDIDATE | CC BY 4.0 | geometry/acoustic bridge |
| Replica | LOCAL_RESEARCH_ONLY by default | research/educational/non-commercial terms | complex indoor mesh |
| ScanNet | LOCAL_RESEARCH_ONLY | gated Terms of Use | real scan stress |
| ScanNet++ | LOCAL_RESEARCH_ONLY | account/application/custom terms | high-fidelity scan research |
| Matterport3D | LOCAL_RESEARCH_ONLY | terms-based | indoor scene stress |
| HM3D | LOCAL_RESEARCH_ONLY | academic/non-commercial | indoor scene stress |
| 3D Warehouse models | USER_IMPORT_ONLY / REJECT_FOR_AGGREGATION | terms restrict aggregation/redistribution | local project visual geometry |

## 8. Video / calibration / instrument ledger

### UMIK

- UMIK-1/2 serial-specific calibration: **USER_INSTANCE_IMPORT** —
  source is the miniDSP official serial lookup; never bundle another
  user's calibration file as generic data.

### EDID

- linuxhw/EDID: **BUNDLE/TEST-CORPUS_CANDIDATE**, CC BY 4.0 — parser
  fixtures only; not nominal product truth.

### Video colorimetry

- Argyll CTI3 `.ti3` / `.cal` / CCMX / CCSS formats: **READY_FOR_ADMISSION_REVIEW**
  as `DOCUMENTED_TEXT_INTERCHANGE`.
- Argyll code: AGPLv3 — an independent parser is preferred.
- DisplayCAL correction DB entries: **PUBLIC_DOMAIN_COMMUNITY**, but
  quality/admissibility remains user-selected; never auto-applied.
- Spears & Munsil media: **USER_OWNED_EXTERNAL_MEDIA** — no bundling.

### Speaker impedance

- REW `.zma`/text impedance magnitude+phase import path: **USER_MEASURED** —
  the preferred path for #544; never scrape impedance graphs. Bundle no
  user-measured data.

### Projector / screen

| Asset | State | License/rights found | Proposed use |
|---|---|---|---|
| JVC DLA-NZ500/NZ900 family spec pages + PDFs | READY_FOR_ADMISSION_REVIEW (field extraction) | manufacturer primary documents | projector zoom/throw/shift capability evidence for #415 |
| Epson model/lens throw-distance tables + simulator | LINK_ONLY | manufacturer tool/table; not a redistributable dataset | per-model exact geometry reference |
| Sony projector installation/throw simulator | LINK_ONLY | manufacturer tool | geometry reference; user supplies values |
| ProjectorCentral database | THIRD_PARTY_DISCOVERY_ONLY | copyright policy permits limited personal use; online reproduction needs permission | model discovery only — no bulk scrape; canonical values from manufacturer evidence |
| Stewart Filmscreen material pages (gain, half-gain, min throw, AT claims) | LICENSE_REVIEW_REQUIRED | manufacturer claims; field extraction rights unreviewed | screen optical profile for #631 as manufacturer evidence |
| Seymour AV Center Stage acoustic-transparent/attenuation claims | LICENSE_REVIEW_REQUIRED | manufacturer claims, no published test method details | AT evidence stays manufacturer-claimed until a test method is recorded |

- Manufacturer primary documents: **LINK/EXTRACT_WITH_PROVENANCE**.
- Screen gain/half-gain/acoustic rows keep `MANUFACTURER_DECLARED`
  provenance; independent measurement supersedes only through explicit
  new evidence — never convert prose into high-resolution curves.

### Speaker / subwoofer usable-output datasets (#648)

| Asset | State | License/rights found | Proposed use |
|---|---|---|---|
| Data-Bass tabulated CEA-2010 max-output | PERMISSION_REQUIRED | no explicit redistribution grant found | subwoofer output-capability discovery + method comparison; preserve protocol/distance/RMS-peak/environment if admitted |
| Erin's Audio Corner compression/distortion reviews | THIRD_PARTY_DISCOVERY_ONLY | copyrighted review graphs/articles | method research + user-linked evidence only; no plot digitization without permission |
| Manufacturer max-output specs | USER_IMPORT_CANDIDATE | manufacturer primary documents | declared-level evidence input, never silently upgraded to measured |

## 9. Device / software interoperability ledger

| Target | State | Why |
|---|---|---|
| Equalizer APO config | READY_FOR_ADMISSION_REVIEW | documented deterministic text grammar |
| CamillaDSP YAML/API | READY_FOR_ADMISSION_REVIEW | public config + live WebSocket readback |
| Yamaha RX-A4A docs/FW | DOCUMENTATION_SOURCE_READY | official specs/manual/FW available |
| Yamaha live API/control | HARDWARE/PROTOCOL_REVIEW_REQUIRED | official current protocol evidence incomplete |
| miniDSP product control | PRODUCT_SPECIFIC_RESEARCH | no generic all-miniDSP claim |
| Audyssey artifacts | PROPRIETARY_FORMAT_RESEARCH | user-supplied only when legal/understood |
| Dirac/Trinnov proprietary artifacts | LINK/OBSERVED_STATE_ONLY by default | no assumption of writable public format/API |

## 10. OSS reuse ledger

Code-license and binary/runtime/sample-data terms are reviewed
independently for every candidate.

### Material / acoustics

| Package | License | Role |
|---|---|---|
| acoustipy | MIT | SOFTWARE_REUSE_BAKEOFF |
| phonometry | MIT | SOFTWARE_REUSE_BAKEOFF / reference oracle |
| pyva | MIT | later reference |
| python-acoustics | BSD-3-Clause (archived) | reference oracle |
| pyroomacoustics | MIT | independent reference/provider |

### Geometry

| Package | License | Role |
|---|---|---|
| trimesh | MIT | high-priority |
| meshio | MIT | import-format bakeoff |
| Open3D | MIT | scan/registration candidate |
| manifold3d | Apache-2.0 | bounded manifold Boolean/CAD |
| PyMeshLab | GPL-3.0 | external research by default |
| CGAL PMP | GPL/commercial | explicit licensing decision |

### Solver / acceleration

| Package | License | Role |
|---|---|---|
| PFFDTD | MIT (sample assets separate) | wave candidate/reference |
| Bempp-cl | MIT | BEM research/reference |
| Embree | Apache-2.0 | CPU ray kernel |
| NVIDIA OptiX | NVIDIA SDK/EULA | optional GPU backend |
| Gmsh | GPL/commercial | license-sensitive mesher |

### EDID

| Package | License | Role |
|---|---|---|
| libdisplay-info | MIT | preferred parser/reuse candidate |
| edid-decode | MIT | independent oracle |

## 11. Standards ledger

Standards are never dataset-bundling candidates by default. Every
ISO, IEC, CTA, ANSI/ASA, ASTM, ITU-R, SMPTE, Dolby and CEDIA source
tracked in #807 defaults to **STANDARDS_REFERENCE_ONLY**.

For every source record:

- exact edition/date;
- current/superseded/draft state;
- target population;
- access terms;
- whether numeric criterion extraction is legally/technically reviewed.

Do not upload paid standards PDFs to the repository. A free download does
not automatically grant redistribution.

## 12. Admission review checklist

Before a source moves to BUNDLE_CANDIDATE / approved fixture, record:

### Identity

- canonical source URL/DOI;
- publisher/provider;
- dataset/version/date;
- exact files selected;
- SHA-256.

### Rights

- license text/source;
- attribution;
- redistribution;
- modification/derivative rights;
- non-commercial restriction?
- share-alike/copyleft effect?
- source-code vs data-license distinction.

### Technical semantics

- quantity;
- units;
- coordinates;
- reference level;
- test method;
- standard/version;
- frequency/angular domain;
- missing data;
- uncertainty;
- source/receiver/material configuration.

### Product claim mapping

- claims enabled;
- claims explicitly not enabled;
- which authority type receives it;
- whether it is measured/modelled/manufacturer-declared/community.

### Storage

- bundle;
- bounded fixture;
- download on demand;
- local cache;
- user import;
- link only.

### Maintenance

- upstream version check;
- supersession;
- withdrawal handling;
- parser version;
- project exact-version retention.

## 13. Permission / contact backlog

High-value ambiguous sources must not sit at "license unclear"
indefinitely. Bounded follow-up research records are owed for:

- BRAS exact database rights/endpoint;
- dEchorate dataset rights;
- RPG CSV redistribution;
- Acoustic Index API/cache/redistribution terms;
- selected treatment manufacturer raw data;
- selected Spinorama original measurement providers;
- scattering database author permission;
- RAVes license clarification.

A permission request must state:

- HTDT purpose;
- personal/open-source product context;
- exactly what subset is desired;
- whether raw data or transformed fixture is requested;
- attribution plan;
- no implied endorsement.

Do not contact vendors/authors without an explicit future action request
where communication requires user identity/account. This ledger only
records what should be asked.

## Rules this ledger enforces

- every researched external asset has exactly one admission state;
- "downloadable" is never used as a synonym for "redistributable";
- source-code and data licenses are tracked separately;
- restricted/non-commercial assets cannot enter normal distributable
  fixtures silently;
- standards are source references unless explicit redistribution
  permission exists;
- bundle candidates retain exact file/license/attribution before payload
  admission;
- large open datasets default to DOI/hash/download-on-demand or a
  bounded subset;
- user-instance/private assets remain private/user-imported;
- each admitted asset states what claims it cannot support;
- ambiguous high-value sources stay visible in the permission/license
  backlog instead of being forgotten.

## Non-goals

- legal advice;
- automatically accepting licenses;
- contacting vendors/authors;
- downloading external payloads;
- implementing parsers;
- mirroring third-party databases.
