# Sincronizarea automată a template-urilor DOCX în OrtoDocs

OrtoDocs sincronizează offline template-urile `.docx` editate în Microsoft Word dintr-un folder local ales din **Setări → Template-uri DOCX**.

## 1. Structura folderului

Exemplu recomandat:

```text
OrtoDocs_Templates/
├── catalog.json
├── epicrize/
│   ├── coxartroza_bilaterala.docx
│   └── gonartroza_stanga.docx
└── protocoale/
    ├── proteza_totala_sold.docx
    └── proteza_totala_genunchi.docx
```

Toate căile din `catalog.json` trebuie să fie căi relative în interiorul folderului rădăcină (fără `..`, căi absolute, unități de disc sau symlink-uri).

## 2. Structura fișierului `catalog.json` (v1)

Vedeți exemplul complet în [`catalog.example.json`](catalog.example.json).

- `catalog_version`: trebuie să fie `1`.
- `diagnoses`: listă de diagnostice, fiecare asociat template-ului **Epicriză + Recomandări**:
  - `key`: identificator stabil ASCII (`[a-z0-9][a-z0-9_-]*`) folosit pentru maparea persistentă și pentru referințele din `interventions`.
  - `name`: denumirea afișată a diagnosticului.
  - `code`: cod medical opțional (sau `null`).
  - `epicrisis_path`: calea relativă către fișierul `.docx` pentru Epicriză + Recomandări.
- `interventions`: listă de intervenții, fiecare asociată unui diagnostic și template-ului **Protocol operator**:
  - `key`: identificator stabil ASCII.
  - `diagnosis_key`: cheia unui diagnostic din lista `diagnoses`.
  - `name`: denumirea afișată a intervenției.
  - `code`: cod opțional (sau `null`).
  - `protocol_path`: calea relativă către fișierul `.docx` pentru Protocol operator.

## 3. Reguli de sincronizare

1. **Pornire & buton manual**: OrtoDocs scanează automat folderul configurat la pornire și la apăsarea butonului **Sincronizează acum** din **Setări → Template-uri DOCX**.
2. **Detecție prin hash SHA-256**: fiecare fișier DOCX este comparat cu versiunea activă anterioară. Fișierele neschimbate nu creează versiuni noi.
3. **Validare strictă în lot**: toate fișierele noi sau modificate trebuie să treacă parsarea fără **nicio eroare** și fără **niciun avertisment** (inclusiv interdicția variabilelor de identificare pacient precum `nume_pacient`, `cnp`, etc.). Dacă un singur document are erori sau avertismente, **întreaga sincronizare este oprită** și baza de date rămâne neschimbată.
4. **Confirmare unică**: dacă toate modificările sunt valide, aplicația afișează un singur dialog de confirmare cu declarația privind absența datelor de identificare ale pacienților și butoanele **Confirm și actualizează** / **Anulează**.
5. **Backup automat și tranzacție atomică**: înainte de scrierea modificărilor, OrtoDocs creează un backup SQLite verificat în subdirectorul `backups/` de lângă baza de date, apoi execută întregul import și activarea versiunilor într-o singură tranzacție atomică (`ROLLBACK` complet la orice eroare).
6. **Istoric păstrat**: versiunile anterioare rămân în istoric (`retired`), iar diagnosticele/intervențiile deja existente nu sunt șterse dacă un fișier lipsește din catalog.
7. **Secțiunea „Șabloane”**: permite administrarea diagnosticelor/intervențiilor și afișează **read-only** versiunea activă, numărul de versiuni istorice, sursa DOCX, data sincronizării și previzualizarea.
