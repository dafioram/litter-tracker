# Litter Tracker 🐈

**Litter Tracker** is a self-hosted data dashboard for the Litter-Robot 4.

While the official Whisker app limits you to 30 days of history, Litter Tracker stores your data locally forever. It allows you to track weight trends, visit frequency, and bathroom habits over the lifespan of your cats, providing valuable insights for vet visits and health monitoring.

## 🌟 Key Features

* **Unlimited History:** Break free from the 30-day limit. Import your data once, and keep it forever to spot long-term weight or frequency trends.
* **Smart Cat Recognition:** Uses a "Nearest Neighbor" algorithm to automatically assign visits to specific cats based on their weight profile.
* **Vet Reports:** Generate a clean, printable summary of a cat's weight and bathroom frequency for your next vet appointment.
* **Data Correction:**
* **Editor:** Easily reassign visits if the robot guessed the wrong cat.
* **Blacklist:** Permanently ignore specific sensor errors or ghost readings without deleting the raw data.


* **Duplicate Protection:** Smart import logic prevents duplicate entries, so you can upload overlapping CSV files without worry.
* **Automatic Backups:** The database is automatically backed up every time you successfully import a new CSV.
* **Mobile Friendly:** The dashboard is responsive and works great on phone browsers.

## ⚠️ Important Notes

* **CSV Import Only:** This application is designed to parse the CSV export file from the Whisker mobile app. There is currently no option to manually enter weight or visit data by hand.
* **Device Compatibility:** This project has currently only been tested with data from the **Litter-Robot 4**.

## 🚀 Getting Started (Docker)

The easiest way to run Litter Tracker is using Docker.

### 1. Installation

Clone this repository and navigate to the folder:

```bash
git clone https://github.com/dafioram/litter-tracker.git
cd litter-tracker

```

### 2. Configuration

Create your configuration file by copying the example. You can edit `.env` to change the port, timezone, or security key.

```bash
cp .env.example .env

```

**Recommended `.env` settings:**

* `TIMEZONE_OFFSET`: Set this to match your local time (e.g., `5` for EST). The CSV data is in UTC, so this corrects the charts to your wall-clock time.
* `PORT`: Default is 5000. Change this if you want to access the dashboard on a different port.

### 3. Run the App

Start the container in the background:

```bash
docker compose up -d --build

```

Access the dashboard at: **http://localhost:5000** (or your server IP).

---

## 📖 How to Use

### 1. Export Data

1. Open the Whisker App on your phone.
2. Go to **History** -> **Download** (icon usually at the top right).
3. Select a date range (e.g., Last 30 Days).
4. Save the CSV file to your computer.

### 2. Setup Cats

1. On the Litter Tracker dashboard, use the **"Manage Cat Profiles"** card.
2. Add a profile for each cat (Name, Target Weight, Birthday).
3. *Note: You must add at least one cat profile before the system will allow you to upload data.*

### 3. Import & Analyze

1. Upload your CSV file on the dashboard.
2. The system will automatically classify entries based on the weights you set.
3. Use the **Trends** tab to view health charts or the **Review** tab to fix any "Unknown" or "Error" entries.

---

## 🛠️ Tech Stack

* **Backend:** Python (Flask, Pandas, SQLite)
* **Frontend:** HTML/CSS (Responsive), Chart.js
* **Containerization:** Docker & Docker Compose

## 📄 License

[MIT License](https://www.google.com/search?q=LICENSE) - Free to fork and modify for your own furry friends.

*Disclaimer: This project is an unofficial utility and is not affiliated with, endorsed by, or connected to Whisker or AutoPets.*