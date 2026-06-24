# syntax=docker/dockerfile:1

# Python 3.12 — ortools liefert (noch) keine Wheels für 3.13/3.14
FROM python:3.12-slim

# Streamlit/Plotly/Folium brauchen keine Compiler; ortools & numpy kommen als Wheels.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Abhängigkeiten zuerst -> Layer-Caching
COPY requirements.txt .
RUN pip install -r requirements.txt

# Anwendungscode
COPY . .

# Streamlit Standardport
EXPOSE 8501

# Headless im Container, lauscht auf allen Interfaces (wichtig hinter Coolify-Proxy)
ENV STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://localhost:8501/_stcore/health').status==200 else sys.exit(1)"

CMD ["streamlit", "run", "app.py"]
