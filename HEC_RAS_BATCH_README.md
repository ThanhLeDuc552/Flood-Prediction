# HEC-RAS 7.0.1 automated unsteady-run script

## What it does

For every CSV placed in the configured discharge-data folder, the script:

1. reads the CSV datetime range;
2. checks that timestamps are regular;
3. detects the HEC-RAS time interval;
4. replaces the mapped `Flow Hydrograph` data in a run-specific copy of `.u01`;
5. sets `Use Fixed Start Time=True` and `Fixed Start Date/Time` to the CSV first timestamp;
6. sets the mapped `Initial Flow Loc` values to the first discharge in the CSV;
7. sets the plan `Simulation Date` to the CSV first and last timestamps;
8. runs HEC-RAS 7.0.1 through `HECRASController` in blocking mode;
9. stores that run's HEC-RAS output in its own subfolder;
10. writes `run_manifest.csv`.

## Important point about this particular model

The supplied `danangfloodmodel.u01` contains three flow-hydrograph boundaries:

- Ai Nghia / Main Reach / 6399
- Tuy Loan / Main Reach / 9800
- Vu Gia / Main Reach / 39160

The script therefore expects the CSV to have columns named:

```text
DateTime,Ai Nghia,Tuy Loan,Vu Gia
```

If your real CSV has different column names, edit `BOUNDARY_MAP` in the script. If your CSV contains only one discharge series, keep only one mapping entry.

The supplied `.u01` also contains initial-flow entries for Cam Le, Han, Lac Thanh, Tuy Loan, Vu Gia, Yen, and Ai Nghia. The script changes only the initial-flow entries explicitly mapped in `BOUNDARY_MAP`; all other initial conditions are left unchanged.

## CSV example

```csv
DateTime,Ai Nghia,Tuy Loan,Vu Gia
2022-10-14 00:00,1360.758,51.039,396.633
2022-10-14 01:00,1469.703,56.954,487.879
2022-10-14 02:00,1578.648,62.869,579.126
...
2022-10-17 12:00,1187.633,104.547,590.977
```

The first row's discharge becomes the corresponding initial flow.

## Folder layout

Recommended:

```text
D:\weather_pred\hec-ras flood modeling\
│
├─ danang flood modelling\          <-- original/template HEC-RAS project
│  ├─ danangfloodmodel.prj
│  ├─ danangfloodmodel.p05
│  ├─ danangfloodmodel.g02
│  ├─ danangfloodmodel.u01
│  └─ ...
│
├─ discharge_data\
│  ├─ event_01.csv
│  ├─ event_02.csv
│  └─ ...
│
└─ automated_runs\
   ├─ event_01_202210140000_202210171200\
   ├─ event_02_202301...\
   └─ run_manifest.csv
```

Each run is isolated so HEC-RAS cannot overwrite the results of another run.

## Installation

On the Windows machine where HEC-RAS 7.0.1 is installed:

```powershell
py -m pip install pywin32
```

Then edit the paths and `BOUNDARY_MAP` near the top of `hec_ras_batch.py`.

Run:

```powershell
py hec_ras_batch.py
```

## HEC-RAS COM identifier

The script uses:

```python
HEC_RAS_PROGID = "RAS701.HECRASController"
```

If that ProgID is not registered on the machine, test/adjust it to the ProgID exposed by your HEC-RAS 7.0.1 installation.

## Safety

The original `.u01` and `.p05` are never modified. The script first creates a run-specific project copy and edits only that copy.

Do not run two instances against the same run directory.

## One limitation to confirm before production runs

This version assumes the CSV represents the complete simulation period and that every configured discharge series uses the same datetime index. If your CSVs instead contain one river per file, the mapping can be simplified accordingly.

The supplied `.u01` was inspected directly and its flow-hydrograph blocks contain 85 ordinates at 1-hour spacing. The script does not hard-code 85; it replaces the ordinate count with the actual CSV length.
