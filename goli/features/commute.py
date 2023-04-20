from typing import Union

import numpy as np

from scipy.sparse import spmatrix, issparse
from scipy.linalg import pinv


def compute_commute_distances(adj: Union[np.ndarray, spmatrix], num_nodes: int) -> np.ndarray:
    """
    Compute avg. commute time/distance between node pairs. This is the avg. number of steps a random walker, starting
    at node i, will take before reaching a given node j for the first time, and then return to node i.
    
    Reference: Saerens et al. "The principal components analysis of a graph, and its relationships to spectral clustering." ECML. 2004.

    Parameters:
        adj [num_nodes, num_nodes]: Adjacency matrix
        num_nodes: Number of nodes in the graph
    Returns:
        dist [num_nodes, num_nodes]: 2D array with avg. commute distances between node pairs
    """
    
    if issparse(adj):
        adj = adj.toarray()

    volG = adj.sum()

    L = np.diagflat(np.sum(adj, axis=1)) - adj
    pinvL = pinv(L)

    dist = volG * np.asarray([[pinvL[i,i] + pinvL[j,j] - 2*pinvL[i,j] for j in range(num_nodes)] for i in range(num_nodes)])
    
    return dist