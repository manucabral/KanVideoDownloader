# KanVideoDownloader

A simple CLI tool to download episodes from [kan.org.il](https://www.kan.org.il).

## Features

- Download single episodes or entire series
- Interactive episode selector (pick individual episodes, ranges, or all)
- Automatic ffmpeg detection (system PATH or bundled binary)

## Requirements

- Python 3.10+
- [ffmpeg](https://ffmpeg.org/download.html) installed and in your PATH (or use `--ffmpeg-path`)

## Installation

```bash
git clone https://github.com/manucabral/KanVideoDownloader.git
cd KanVideoDownloader
pip install -r requirements.txt
```

## Usage

```bash
# Download
python test.py https://www.kan.org.il/content/kan/kan-11/p-829567/s1/840305/

# Custom output directory
python test.py -o downloads https://www.kan.org.il/...

# Verbose
python test.py -v https://www.kan.org.il/...

# Custom ffmpeg path
python test.py -fp /path/to/ffmpeg https://www.kan.org.il/...
```

## Disclaimer

This project is provided **for educational purposes only**.  
The author do **not** assume any responsibility for how this software is used.  
Use it at your own risk and make sure you comply with any applicable terms of service.
