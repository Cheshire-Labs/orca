"""
Unit tests for DeadlockGraph class.
These tests serve as a safety net before Phase 2 refactoring.
"""
import pytest

from orca.system.reservation_manager.deadlock_manager import DeadlockGraph


class TestDeadlockGraph:
    """Test suite for DeadlockGraph class"""

    def test_add_edge_creates_nodes(self):
        """Test that adding an edge creates both nodes in the graph"""
        graph = DeadlockGraph()

        # Add edge A → B
        graph._add_edge("A", "B")

        # Verify both nodes exist in graph
        assert graph._graph.has_node("A")
        assert graph._graph.has_node("B")
        assert graph._graph.has_edge("A", "B")

    def test_is_deadlocked_detects_cycle(self):
        """Test that is_deadlocked returns True when cycle exists"""
        graph = DeadlockGraph()

        # Create cycle: A → B → C → A
        graph._add_edge("A", "B")
        graph._add_edge("B", "C")
        graph._add_edge("C", "A")

        # Verify deadlock is detected
        assert graph.is_deadlocked() is True

    def test_is_deadlocked_no_cycle(self):
        """Test that is_deadlocked returns False when no cycle exists"""
        graph = DeadlockGraph()

        # Create linear dependency: A → B → C (no cycle)
        graph._add_edge("A", "B")
        graph._add_edge("B", "C")

        # Verify no deadlock
        assert graph.is_deadlocked() is False

    def test_find_cycle_nodes_returns_cycle(self):
        """Test that find_cycle_nodes returns nodes in cycle"""
        graph = DeadlockGraph()

        # Create cycle: A → B → C → A
        graph._add_edge("A", "B")
        graph._add_edge("B", "C")
        graph._add_edge("C", "A")

        # Get cycle nodes
        cycle_nodes = graph.find_cycle_nodes()

        # Verify all nodes in cycle are returned
        assert "A" in cycle_nodes
        assert "B" in cycle_nodes
        assert "C" in cycle_nodes
        assert len(cycle_nodes) == 3

    def test_find_cycle_nodes_no_cycle_empty(self):
        """Test that find_cycle_nodes returns empty set when no cycle"""
        graph = DeadlockGraph()

        # Create linear dependency: A → B → C (no cycle)
        graph._add_edge("A", "B")
        graph._add_edge("B", "C")

        # Get cycle nodes
        cycle_nodes = graph.find_cycle_nodes()

        # Verify empty set returned
        assert len(cycle_nodes) == 0

    def test_reset_clears_graph(self):
        """Test that reset() clears all nodes and edges"""
        graph = DeadlockGraph()

        # Add some edges
        graph._add_edge("A", "B")
        graph._add_edge("B", "C")

        # Verify graph has nodes
        assert graph._graph.has_node("A")
        assert graph._graph.has_node("B")

        # Reset
        graph.reset()

        # Verify graph is empty
        assert len(graph._graph.nodes()) == 0
        assert len(graph._graph.edges()) == 0

    def test_self_loop_is_deadlock(self):
        """Test that a self-loop (A → A) is detected as deadlock"""
        graph = DeadlockGraph()

        # Create self-loop: A → A
        graph._add_edge("A", "A")

        # Verify deadlock is detected
        assert graph.is_deadlocked() is True

        # Verify cycle nodes include A
        cycle_nodes = graph.find_cycle_nodes()
        assert "A" in cycle_nodes
