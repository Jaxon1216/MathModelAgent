# Task22 Cleaned Path Provenance

## Scope

Keep `cleaned_relpath_for(source, sheet)` backward compatible. A normal
source-sheet continues to use `cleaned/{sanitized_source}__{sanitized_sheet}.csv`.
Only entries in the same `source_inspection` whose base paths are duplicated get
a deterministic collision suffix based on the inspection entry ID.

## Contract

- `source_inspection.json` declares one `cleaned_path` for every readable or
  inspectable source sheet.
- The collision suffix uses the first eight hexadecimal characters of
  `sha256(inspection_entry_id)`.
- Source file SHA-256 remains in inspection and contract provenance for audit;
  it is not part of the ordinary filename.
- Rendered inspection material and the data-preparation brief show the declared
  path for every source sheet.
- When a source inspection is attached, `data_contract` accepts only an exact
  declared `cleaned_path`. Missing declarations, zero matches, or multiple
  source-sheet matches raise `DataContractError`; no candidate is selected by
  ordering or fuzzy inference.
- Calls without source inspection retain the existing filename inference
  behavior for old contracts and isolated utility use.

## Verification

Add an end-to-end regression using `a b.csv` and `a_b.csv`: build and save
inspection, write cleaned files at the declared paths, build/save/validate the
contract, and verify source provenance and unique paths. Also assert that an
unambiguous source keeps the historical path.

Writer, Pandoc, plotting, and unrelated workflow changes are out of scope.
