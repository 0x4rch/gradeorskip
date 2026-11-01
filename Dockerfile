FROM python:3.14-slim
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY gradeorskip gradeorskip
COPY config config
RUN useradd -r app && mkdir /data && chown app /data
USER app
ENV GRADEORSKIP_DATA_DIR=/data
EXPOSE 8790
CMD ["uvicorn", "gradeorskip.app:app", "--host", "0.0.0.0", "--port", "8790"]
