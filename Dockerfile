FROM apify/actor-python:3.13

COPY requirements.txt ./

RUN echo "Python version:" \
    && python --version \
    && echo "Installing dependencies:" \
    && pip install --no-cache-dir -r requirements.txt \
    && echo "All installed Python packages:" \
    && pip freeze

COPY . ./

# Fail fast on a syntax error rather than at run time.
RUN python -m compileall -q fetch.py parse.py extract.py actor_main.py

CMD ["python3", "-m", "actor_main"]
