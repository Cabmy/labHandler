"""固定存储。评测期间不要改这个文件。"""


class Store:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def write(self, key: str, value: str) -> None:
        self.data[key] = value

    def read(self, key: str) -> str | None:
        return self.data.get(key)
