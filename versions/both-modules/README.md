## Both modules (classic + FC)

Запуск этой версии выполняется **только из этой папки**.

### 1) Установка

```bash
pip install -r requirements.txt
```

### 2) Настройка env

Скопируйте `.env.example` в `.env` и заполните ключи:

- `AITUNNEL_API_KEY`
- `TW_QID_TWEET`
- `BINANCE_API_KEY` (или `COINCAP_API_KEY`)

Также нужен `cookies.json` (auth_token + ct0) рядом с `app.py`.

### 3) Запуск

```bash
python app.py
```

### Примечание

Если локальная модель классификатора отсутствует, используется встроенный heuristic fallback.
