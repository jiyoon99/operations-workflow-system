# GitHub upload checklist

Before publishing this project, keep the repository source-only.

- Do not commit `data/`, `backups/`, `tms-export/`, or any `venv*` directory.
- Do not commit SQLite DB files, Excel exports, logs, or `.env` files.
- Recreate the virtual environment on each PC:

```bat
python -m venv venv
venv\Scripts\pip install -r requirements.txt
```

- Run tests after recreating Python:

```bat
venv\Scripts\python.exe -m unittest discover -s tests -v
```

- If the repository will be public, use a fresh empty database and remove any real customer/order/API-key data from examples and screenshots.
