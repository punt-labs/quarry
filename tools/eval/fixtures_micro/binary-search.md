# Binary search

Binary search finds a target value in a sorted array in logarithmic time.
It maintains a range that must contain the target, repeatedly halving the
range by comparing the target to the middle element. If the middle element
is smaller, the search continues in the upper half; if larger, in the lower
half; if equal, the index is returned.

Binary search runs in O(log n) comparisons because each step discards half
of the remaining candidates. It requires the input to be sorted first,
which is the precondition that makes the halving argument valid.
