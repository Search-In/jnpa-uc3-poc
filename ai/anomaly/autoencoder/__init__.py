"""Trajectory autoencoder package (UC-III Sub-Criterion 2C).

A small 1D-convolutional autoencoder over per-track trajectory features. It is
trained on *normal* corridor trajectories; at inference, a reconstruction error
above the 99th-percentile training threshold flags an ANOMALOUS_TRAJECTORY —
catching behaviours the rule engine cannot enumerate (e.g. slow looping, erratic
weaving) that nonetheless reconstruct poorly against the learned normal manifold.
"""
