# WattLab — Web UI

## Setup

```bash
pip install -r requirements.txt
```

Place `GoSLogo.jpeg` in a `static/` folder alongside `app.py`:

```
wattlab/
├── app.py
├── index.html
├── requirements.txt
└── static/
    └── GoSLogo.jpeg
```

## Run

```bash
python app.py
```

Then open **http://localhost:8080** in your browser.

## Usage

1. Enter your Tapo credentials and select/add the device IP
2. Click **Connect** to verify the device is reachable
3. Set your filename, interval, and duration
4. Click **Start Measurement** — live readings stream to the terminal
5. Click **Stop Measurement** at any time — data collected so far is saved
6. Download any saved CSV from the **Saved Results** panel at the bottom
