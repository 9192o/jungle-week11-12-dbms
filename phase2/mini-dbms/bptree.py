from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field


DEFAULT_MAX_KEYS = 128


class DuplicateKeyError(ValueError):
    pass


@dataclass(slots=True)
class Node:
    keys: list[int] = field(default_factory=list)
    children: list["Node"] | None = None
    offsets: list[int] | None = field(default_factory=list)


class BPlusTree:
    def __init__(self, max_keys=DEFAULT_MAX_KEYS):
        if max_keys < 3:
            raise ValueError("max_keys must be at least 3")
        self.max_keys = max_keys
        self.root = Node()

    def search(self, key):
        node = self.root
        while node.children is not None:
            node = node.children[bisect_right(node.keys, key)]
        position = bisect_left(node.keys, key)
        if position < len(node.keys) and node.keys[position] == key:
            return node.offsets[position]
        return None

    def insert(self, key, offset):
        split = self._insert(self.root, key, offset)
        if split is not None:
            separator, right = split
            self.root = Node([separator], [self.root, right], None)

    def _insert(self, node, key, offset):
        if node.children is None:
            position = bisect_left(node.keys, key)
            if position < len(node.keys) and node.keys[position] == key:
                raise DuplicateKeyError(f"duplicate record_id {key}")
            node.keys.insert(position, key)
            node.offsets.insert(position, offset)
        else:
            position = bisect_right(node.keys, key)
            split = self._insert(node.children[position], key, offset)
            if split is None:
                return None
            separator, right = split
            node.keys.insert(position, separator)
            node.children.insert(position + 1, right)

        if len(node.keys) <= self.max_keys:
            return None

        middle = len(node.keys) // 2
        separator = node.keys[middle]
        if node.children is None:
            right = Node(node.keys[middle:], offsets=node.offsets[middle:])
            del node.offsets[middle:]
        else:
            right = Node(node.keys[middle + 1 :], node.children[middle + 1 :], None)
            del node.children[middle + 1 :]
        del node.keys[middle:]
        return separator, right
