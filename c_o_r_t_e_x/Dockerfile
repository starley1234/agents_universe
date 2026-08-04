FROM python:3.12-slim

WORKDIR /app
COPY . /app/c_o_r_t_e_x
RUN pip install --no-cache-dir -e '/app/c_o_r_t_e_x[api]'

ENV APP_HOST=0.0.0.0 APP_PORT=8117 CORTEX_TOOLKIT_MODE=remote
EXPOSE 8117
CMD ["python", "-m", "c_o_r_t_e_x", "serve"]
