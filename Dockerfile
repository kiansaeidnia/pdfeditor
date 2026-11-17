FROM python:3.12-slim

# Tesseract languages to install, space separated, as Tesseract codes: "eng deu chi_sim"
ARG OCR_LANGS="eng"

# LibreOffice: the -nogui packages convert headless without pulling in a desktop; draw carries the PDF importer.
# pango + harfbuzz-subset are what WeasyPrint loads at run time.
# Fonts: DejaVu, Liberation (metric-compatible with Arial/Times/Courier), Carlito and Caladea (the same for Calibri
# and Cambria, so edited text in Word documents keeps its look) and Droid Sans Fallback for CJK (4 MB;
# swap in fonts-noto-cjk, about 90 MB, for proper CJK typography).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui libreoffice-draw-nogui \
      tesseract-ocr $(printf 'tesseract-ocr-%s ' $(echo "$OCR_LANGS" | tr _ -)) \
      ghostscript \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 \
      fonts-dejavu-core fonts-liberation fonts-droid-fallback fonts-crosextra-carlito fonts-crosextra-caladea \
 && rm -rf /var/lib/apt/lists/*

# LibreOffice and fontconfig need a writable home; /data keeps the settings chosen in the first-run setup
RUN useradd --create-home --uid 1000 app && mkdir /data && chown app:app /data
ENV HOME=/home/app PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 WORKERS=2 DATA_DIR=/data
VOLUME /data

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app

USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --start-interval=2s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)"]
# one PDF job at a time per worker, so WORKERS is the number of jobs that can run side by side
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers \"$WORKERS\""]
