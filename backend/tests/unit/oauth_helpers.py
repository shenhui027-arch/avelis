"""In-memory Redis transaction double for offline OAuth tests."""

import threading


class CounterRedis:
    def __init__(self):
        self.values = {}
        self.expiries = {}
        self.mutex = threading.Lock()
        self.transactions = []

    def pipeline(self, transaction=True):
        assert transaction is True
        return CounterPipeline(self)


class CounterPipeline:
    def __init__(self, redis):
        self.redis = redis
        self.commands = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def incr(self, name):
        self.commands.append(("incr", name))
        return self

    def expire(self, name, seconds, nx=False):
        self.commands.append(("expire", name, seconds, nx))
        return self

    def execute(self):
        result = []
        with self.redis.mutex:
            self.redis.transactions.append(self.commands)
            for command in self.commands:
                if command[0] == "incr":
                    name = command[1]
                    self.redis.values[name] = self.redis.values.get(name, 0) + 1
                    result.append(self.redis.values[name])
                else:
                    _, name, seconds, nx = command
                    if not nx or name not in self.redis.expiries:
                        self.redis.expiries[name] = seconds
                    result.append(True)
        return result
