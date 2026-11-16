import random
import unittest

from mini_dbms.bptree import BPlusTree


class BPlusTreeContractTests(unittest.TestCase):
    def test_default_order_is_64(self) -> None:
        tree = BPlusTree()
        self.assertEqual(tree.order, 64)

    def test_custom_order_is_stored(self) -> None:
        tree = BPlusTree(order=8)
        self.assertEqual(tree.order, 8)

    def test_order_below_minimum_raises(self) -> None:
        with self.assertRaises(ValueError):
            BPlusTree(order=2)

    def test_empty_tree_find_returns_none(self) -> None:
        tree = BPlusTree()
        self.assertIsNone(tree.find(1))

    def test_single_insert_and_find(self) -> None:
        tree = BPlusTree()
        tree.insert(10, 100)
        self.assertEqual(tree.find(10), 100)

    def test_unregistered_key_returns_none(self) -> None:
        tree = BPlusTree()
        tree.insert(10, 100)
        self.assertIsNone(tree.find(20))

    def test_root_is_leaf_after_first_insert(self) -> None:
        tree = BPlusTree()
        tree.insert(1, 10)
        self.assertTrue(tree.root.is_leaf)
        self.assertEqual(tree.root.keys, [1])
        self.assertEqual(tree.root.values, [10])


class BPlusTreeDuplicateTests(unittest.TestCase):
    def test_duplicate_key_raises_value_error(self) -> None:
        tree = BPlusTree()
        tree.insert(5, 50)
        with self.assertRaises(ValueError):
            tree.insert(5, 55)

    def test_duplicate_preserves_original_value(self) -> None:
        tree = BPlusTree()
        tree.insert(5, 50)
        try:
            tree.insert(5, 55)
        except ValueError:
            pass
        self.assertEqual(tree.find(5), 50)


class BPlusTreeInsertionOrderTests(unittest.TestCase):
    def test_ascending_insertion(self) -> None:
        tree = BPlusTree(order=4)
        expected: dict[int, int] = {}
        for key in range(1, 51):
            offset = key * 10
            tree.insert(key, offset)
            expected[key] = offset
        for key, offset in expected.items():
            self.assertEqual(tree.find(key), offset)

    def test_descending_insertion(self) -> None:
        tree = BPlusTree(order=4)
        expected: dict[int, int] = {}
        for key in range(50, 0, -1):
            offset = key * 10
            tree.insert(key, offset)
            expected[key] = offset
        for key, offset in expected.items():
            self.assertEqual(tree.find(key), offset)

    def test_shuffled_insertion_with_fixed_seed(self) -> None:
        tree = BPlusTree(order=8)
        expected: dict[int, int] = {}
        generator = random.Random(20261116)
        keys = list(range(1, 201))
        generator.shuffle(keys)
        for key in keys:
            offset = key * 7
            tree.insert(key, offset)
            expected[key] = offset
        for key, offset in expected.items():
            self.assertEqual(tree.find(key), offset)

    def test_negative_keys(self) -> None:
        tree = BPlusTree(order=4)
        expected: dict[int, int] = {}
        for key in range(-50, 51):
            offset = key + 1000
            tree.insert(key, offset)
            expected[key] = offset
        for key, offset in expected.items():
            self.assertEqual(tree.find(key), offset)


class BPlusTreeStructureTests(unittest.TestCase):
    def _assert_invariants(self, tree: BPlusTree) -> None:
        self.assertGreaterEqual(tree.order, 3)
        max_keys = tree.order - 1

        def check_node(node, depth: int) -> int:
            self.assertTrue(hasattr(node, "is_leaf"))
            self.assertTrue(hasattr(node, "keys"))
            self.assertIsInstance(node.keys, list)
            self.assertLessEqual(len(node.keys), max_keys)

            if node.is_leaf:
                self.assertTrue(hasattr(node, "values"))
                self.assertEqual(len(node.values), len(node.keys))
                self.assertEqual(node.keys, sorted(node.keys))
                return depth
            else:
                self.assertTrue(hasattr(node, "children"))
                self.assertEqual(len(node.children), len(node.keys) + 1)
                if node is not tree.root:
                    self.assertGreaterEqual(len(node.children), (tree.order + 1) // 2)
                self.assertEqual(node.keys, sorted(node.keys))
                leaf_depth: int | None = None
                for child in node.children:
                    child_depth = check_node(child, depth + 1)
                    if leaf_depth is None:
                        leaf_depth = child_depth
                    else:
                        self.assertEqual(child_depth, leaf_depth)
                for index in range(len(node.keys)):
                    separator = node.keys[index]
                    right_min = self._minimum_key(node.children[index + 1])
                    self.assertEqual(right_min, separator)
                return leaf_depth if leaf_depth is not None else depth

        if tree.root is None:
            return
        check_node(tree.root, 0)

    def _minimum_key(self, node) -> int | None:
        while not node.is_leaf:
            node = node.children[0]
        return node.keys[0] if node.keys else None

    def _range_invariant(self, tree: BPlusTree) -> None:
        def check(node, lower: int | None, upper: int | None) -> None:
            if node.is_leaf:
                for key in node.keys:
                    if lower is not None:
                        self.assertGreaterEqual(key, lower)
                    if upper is not None:
                        self.assertLess(key, upper)
                return
            for index, child in enumerate(node.children):
                child_lower = lower if index == 0 else node.keys[index - 1]
                child_upper = upper if index == len(node.children) - 1 else node.keys[index]
                check(child, child_lower, child_upper)

        if tree.root is None:
            return
        check(tree.root, None, None)

    def _collect_all(self, tree: BPlusTree) -> dict[int, int]:
        result: dict[int, int] = {}
        if tree.root is None:
            return result
        node = tree.root
        while not node.is_leaf:
            node = node.children[0]
        while node is not None:
            for key, value in zip(node.keys, node.values):
                self.assertNotIn(key, result)
                result[key] = value
            node = getattr(node, "next_leaf", None)
        return result

    def test_structure_invariants_after_ascending_inserts(self) -> None:
        tree = BPlusTree(order=4)
        for key in range(1, 101):
            tree.insert(key, key * 3)
            self._assert_invariants(tree)
            self._range_invariant(tree)

    def test_structure_invariants_after_descending_inserts(self) -> None:
        tree = BPlusTree(order=4)
        for key in range(100, 0, -1):
            tree.insert(key, key * 3)
            self._assert_invariants(tree)
            self._range_invariant(tree)

    def test_structure_invariants_after_shuffled_inserts(self) -> None:
        tree = BPlusTree(order=8)
        generator = random.Random(20261116)
        keys = list(range(1, 201))
        generator.shuffle(keys)
        for key in keys:
            tree.insert(key, key * 7)
            self._assert_invariants(tree)
            self._range_invariant(tree)

    def test_all_values_collected_through_leaves(self) -> None:
        tree = BPlusTree(order=4)
        expected: dict[int, int] = {}
        for key in range(1, 101):
            tree.insert(key, key * 5)
            expected[key] = key * 5
        self.assertEqual(self._collect_all(tree), expected)


class BPlusTreeSplitTests(unittest.TestCase):
    def test_leaf_split_keeps_all_keys(self) -> None:
        tree = BPlusTree(order=4)
        for key in range(1, 8):
            tree.insert(key, key * 10)
        self.assertFalse(tree.root.is_leaf)
        for key in range(1, 8):
            self.assertEqual(tree.find(key), key * 10)

    def test_internal_split_keeps_all_keys(self) -> None:
        tree = BPlusTree(order=4)
        for key in range(1, 21):
            tree.insert(key, key * 10)
        self.assertFalse(tree.root.is_leaf)
        for key in range(1, 21):
            self.assertEqual(tree.find(key), key * 10)

    def test_deep_root_split_height_increases(self) -> None:
        tree = BPlusTree(order=4)
        for key in range(1, 51):
            tree.insert(key, key * 10)
        height = 0
        node = tree.root
        while not node.is_leaf:
            height += 1
            node = node.children[0]
        self.assertGreaterEqual(height, 2)
        for key in range(1, 51):
            self.assertEqual(tree.find(key), key * 10)

    def test_large_order_root_split(self) -> None:
        tree = BPlusTree(order=8)
        for key in range(1, 101):
            tree.insert(key, key * 10)
        self.assertFalse(tree.root.is_leaf)
        for key in range(1, 101):
            self.assertEqual(tree.find(key), key * 10)


class BPlusTreeSeparatorTests(unittest.TestCase):
    def test_equal_key_goes_to_right_child(self) -> None:
        tree = BPlusTree(order=4)
        for key in (1, 2, 3, 4, 5):
            tree.insert(key, key * 10)
        self.assertEqual(tree.find(3), 30)
        self.assertEqual(tree.find(4), 40)
        self.assertEqual(tree.find(5), 50)


if __name__ == "__main__":
    unittest.main()
