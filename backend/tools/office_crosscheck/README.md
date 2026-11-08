# Office cross-check (Windows + Microsoft Office only)

An **independent** audit of the template analyzers. Instead of trusting python-pptx / python-docx /
openpyxl (the libraries the analyzers are built on), these scripts ask Microsoft PowerPoint, Word and
Excel themselves (via COM, read-only, hidden windows) and compare their answers with the analyzer output.

This is how the "bold lost in `.xls` conversion" bug was found: the library-based audit used the same
(wrong) field as the converter, so only Excel's own answer exposed it.

## PowerPoint

```powershell
powershell -NoProfile -File pptx_truth.ps1 -Path "C:\path\deck.potx" -Out truth.json
python compare_pptx.py truth.json "C:\path\deck.potx"
```
Compares slide size, layout names, slide count and every shape's position/size (tolerance 0.2 pt).
Result on the 5 bundled Office templates: 175 shapes, 0 mismatches.

## Word + Excel (the bundled SEC samples)

```powershell
powershell -NoProfile -File office_truth.ps1 -Docx ..\..\..\sample_templates\docx\sec_filing_sample.docx `
    -Xls ..\..\..\sample_templates\xlsx\sec_filing_sample.xls -Out truth_office.json
python compare_word_excel.py truth_office.json
```
Word: page size, margins, table/section counts. Excel: sheet count, column widths, and font / size / bold /
number format / merge state of 252 cells (merged cells are read through their anchor, as Excel does).

Known, benign differences: Excel's `ColumnWidth` excludes ~0.6 chars of padding that the stored width
includes; Word's paragraph count includes end-of-row marks; number formats differ only in escaping/case.
