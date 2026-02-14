# Crane manuals database

Έγινε δημιουργία SQLite βάσης (`cranes.db`) από τα PDF load charts/manuals του repository,
με **δεδομένα ανά διαμόρφωση** και **σημεία load chart** (όπου κατέστη δυνατό από το extracted text).

## Τι περιέχει

- `build_crane_database.py`: script που σκανάρει όλα τα `*.pdf`, εξάγει κείμενο από PDF streams και δημιουργεί 3 πίνακες.
- `cranes.db`: η SQLite βάση.
- `crane_config_point_counts.csv`: πλήθος load points ανά διαμόρφωση/μοντέλο.
- `crane_load_points_sample.csv`: δείγμα εξαγμένων σημείων load chart.

## Σχήμα βάσης

### 1) `crane_manuals`
- `file_name`
- `manufacturer`
- `model`
- `estimated_page_count`
- `file_size_bytes`
- `sha256`
- `extracted_text_sample`

### 2) `crane_configurations`
- `manual_id`
- `config_code`
- `description`

### 3) `load_chart_points`
- `manual_id`
- `config_code`
- `boom_length_m`
- `radius_m`
- `capacity_t`
- `raw_snippet`

## Εκτέλεση

```bash
python build_crane_database.py
```

> Note: `cranes.db` is generated locally and is not committed because binary files are not supported in code review diffs.

## Παράδειγμα query: max capacity ανά configuration

```sql
SELECT m.file_name, p.config_code, MAX(p.capacity_t) AS max_capacity_t
FROM load_chart_points p
JOIN crane_manuals m ON m.id = p.manual_id
GROUP BY m.file_name, p.config_code
ORDER BY max_capacity_t DESC;
```

## Σημείωση ποιότητας δεδομένων

Η εξαγωγή γίνεται χωρίς εξωτερικές PDF βιβλιοθήκες (μέσω stream parsing + regex), άρα τα πεδία
είναι **semi-structured extraction** και χρειάζονται validation για κρίσιμη/παραγωγική χρήση.
