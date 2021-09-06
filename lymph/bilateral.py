import numpy as np
import scipy as sp 
import scipy.stats
import pandas as pd
import warnings
from typing import Union, Optional, List, Dict, Any

from .node import Node
from .edge import Edge
from .unilateral import System


# I chose not to make this one a child of System, since it is basically only a 
# container for two System instances
class BilateralSystem(object):
    """Class that models metastatic progression in a lymphatic system 
    bilaterally by creating two :class:`System` instances that are symmetric in 
    their connections. The parameters describing the spread probabilities 
    however need not be symmetric.

    Args:
        graph: Dictionary of the same kind as for initialization of 
            :class:`System`. This graph will be passed to the constructors of 
            two :class:`System` attributes of this class.
            
        base_symmetric: If ``True``, the spread probabilities of the two 
            sides from the tumor(s) to the LNLs will be set symmetrically.
            
        trans_symmetric: If ``True``, the spread probabilities among the 
            LNLs will be set symmetrically.
            
    See Also:
        :class:`System`: Two instances of this class are created as attributes.
    """
    def __init__(self, 
                 graph: dict = {},
                 base_symmetric: bool = False,
                 trans_symmetric: bool = True):
        self.system = {}
        self.system["ipsi"] = System(graph=graph)   # ipsilateral and...
        self.system["contra"] = System(graph=graph)   # ...contralateral part of the network
        
        # sort all the edges into the two sides (ipsi & contra, first index) 
        # and into base (from tumor to LNL) & trans (from LNL to LNL) along the 
        # second index. 
        self.edges = np.empty(shape=(2,2), dtype=list)
        
        # ipsi
        ipsi_base, ipsi_trans = [], []
        for e in self.system["ipsi"].edges:
            if e.start.typ == 'tumor':
                ipsi_base.append(e)
            elif e.start.typ == 'lnl':
                ipsi_trans.append(e)
            else:
                raise Exception(f"Node {e.start.name} has no correct type.")
        self.edges[0,0] = ipsi_base
        self.edges[0,1] = ipsi_trans
        
        # contra
        contra_base, contra_trans = [], []
        for e in self.system["contra"].edges:
            if e.start.typ == 'tumor':
                contra_base.append(e)
            elif e.start.typ == 'lnl':
                contra_trans.append(e)
            else:
                raise Exception(f"Node {e.start.name} has no correct type.")
        self.edges[1,0] = contra_base
        self.edges[1,1] = contra_trans
        
        self.base_symmetric = base_symmetric
        self.trans_symmetric = trans_symmetric
    
    
    def __str__(self):
        string = "### IPSILATERAL ###\n"
        string += self.system["ipsi"].__str__()
        string += "\n### CONTRALATERAL ###\n"
        string += self.system["contra"].__str__()
        return string
    
    
    @property
    def state(self) -> np.ndarray:
        """
        Return the currently state (healthy or involved) of all LNLs in the 
        system.
        """
        ipsi_state = self.system["ipsi"].state
        contra_state = self.system["contra"].state
        return np.concatenate([ipsi_state, contra_state])
    
    
    @state.setter
    def state(self, newstate: np.ndarray):
        """
        Set the state of the system to ``newstate``.
        """
        self.system["ipsi"].state = newstate[:len(self.system["ipsi"].lnls)]
        self.system["contra"].state = newstate[len(self.system["ipsi"].lnls):]
    
    
    @property
    def spread_probs(self) -> np.ndarray:
        """
        Return the spread probabilities of the :class:`Edge` instances in 
        the network. Length and structure of ``theta`` depends on the set 
        symmetries of the network.
        
        See Also:
            :meth:`theta`: Setting the spread probabilities and symmetries.
        """
        spread_probs = np.array([], dtype=float)
        switch = [self.base_symmetric, self.trans_symmetric]
        for edge_type in [0,1]:
            tmp = [e.t for e in self.edges[0,edge_type]]
            spread_probs = np.append(spread_probs, tmp.copy())
            if not switch[edge_type]:
                tmp = [e.t for e in self.edges[1,edge_type]]
                spread_probs = np.append(spread_probs, tmp.copy())
        
        return spread_probs
    

    @spread_probs.setter
    def spread_probs(self, spread_probs: np.ndarray):
        """
        Set the spread probabilities of the :class:`Edge` instances in the 
        the network.
        """
        switch = [self.base_symmetric, self.trans_symmetric]
        cursor = 0
        for edge_type in [0,1]:
            for side in [0,1]:
                num = len(self.edges[side,edge_type])
                tmp = spread_probs[cursor:cursor+num]
                for i,e in enumerate(self.edges[side,edge_type]):
                    e.t = tmp[i]
                if switch[edge_type]:
                    for i,e in enumerate(self.edges[1,edge_type]):
                        e.t = tmp[i]
                    cursor += num
                    break
                else:
                    cursor += num
        
        for side in ["ipsi", "contra"]:
            try:
                self.system[side]._gen_A()
            except AttributeError:
                n = len(self.system[side].state_list)
                self.system[side].A = np.zeros(shape=(n,n))
                self.system[side]._gen_A()


    @property
    def modalities(self):
        """
        Compute the two system's observation matrices 
        :math:`\\mathbf{B}^{\\text{i}}` and :math:`\\mathbf{B}^{\\text{c}}`.
                
        See Also:
            :meth:`System.set_modalities`: Setting modalities in unilateral System.
        """
        ipsi_modality_spsn = self.system["ipsi"].modalities
        if ipsi_modality_spsn != self.system["contra"].modalities:
            msg = ("Ipsi- & contralaterally stored modalities are not the same")
            raise RuntimeError(msg)
        
        return ipsi_modality_spsn
    
    
    @modalities.setter
    def modalities(self, modality_spsn: Dict[Any, List[float]]):
        """
        Given specificity :math:`s_P` & sensitivity :math:`s_N` of different 
        diagnostic modalities, compute the system's two observation matrices 
        :math:`\\mathbf{B}_i` and :math:`\\mathbf{B}_c`.
        """
        self.system["ipsi"].modalities = modality_spsn
        self.system["contra"].modalities = modality_spsn
    
    
    def load_data(self,
                  data: pd.DataFrame, 
                  t_stages: List[int] = [1,2,3,4], 
                  modality_spsn: Dict[str, List[float]] = {"path": [1., 1.]}, 
                  mode: str = "HMM"):
        """
        Args:
            data: Table with rows of patients. Columns must have three levels. 
                The first column is ('Info', 'tumor', 'T-stage'). The rest of 
                the columns are separated by modality names on the top level, 
                then subdivided into 'ipsi' & 'contra' by the second level and
                finally, in the third level, the names of the lymph node level 
                are given.
        
        See Also:
            :meth:`System.load_data`: Data loading method of unilateral system.
            
            :meth:`System._gen_C`: Generate marginalization matrix.
        """
        # split the DataFrame into two, one for ipsi-, one for contralateral
        ipsi_data = data.drop(
            columns=["contra"], axis=1, level=1, inplace=False
        )
        ipsi_data = pd.DataFrame(
            ipsi_data.values,
            index=ipsi_data.index, columns=ipsi_data.columns.droplevel(1)
        )
        contra_data = data.drop(
            columns=["ipsi"], axis=1, level=1, inplace=False
        )
        contra_data = pd.DataFrame(
            contra_data.values,
            index=contra_data.index, columns=contra_data.columns.droplevel(1)
        )
        
        # generate both side's C matrix with duplicates and ones
        gen_C_kwargs = {'delete_ones': False, 'aggregate_duplicates': False}
        self.system["ipsi"].load_data(
            ipsi_data, 
            t_stages=t_stages,
            modality_spsn=modality_spsn,
            mode=mode,
            gen_C_kwargs=gen_C_kwargs
        )
        self.system["contra"].load_data(
            contra_data, 
            t_stages=t_stages, 
            modality_spsn=modality_spsn,
            mode=mode,
            gen_C_kwargs=gen_C_kwargs
        )
    
    
    def _spread_probs_are_valid(self, new_spread_probs: np.ndarray) -> bool:
        """Check that the spread probability (rates) are all within limits.
        """
        if new_spread_probs.shape != self.spread_probs.shape:
            msg = ("Shape of provided spread parameters does not match network")
            raise ValueError(msg)
        if np.any(np.greater(0., new_spread_probs)):
            return False
        if np.any(np.greater(new_spread_probs, 1.)):
            return False
        
        return True

    
    def marg_likelihood(
        self, 
        theta: np.ndarray, 
        t_stages: List[int] = ["early", "late"], 
        time_dists: dict = {}, 
        mode: str = "HMM"
    ) -> float:
        """
        Compute the likelihood of the (already stored) data, given the spread 
        parameters, marginalized over time of diagnosis via time distributions.

        Args:
            theta: Set of parameters, consisting of the base probabilities 
                :math:`b` (as many as the system has nodes) and the transition 
                probabilities :math:`t` (as many as the system has edges).

            t_stages: List of T-stages that should be included in the learning 
                process.

            time_dists: Distribution over the probability of diagnosis at 
                different times :math:`t` given T-stage.

            mode: ``"HMM"`` for hidden Markov model and ``"BN"`` for Bayesian 
                network (not yet implemented).

        Returns:
            The log-likelihood of a parameter sample.
        """
        if not self._spread_probs_are_valid(theta):
            return -np.inf
        
        self.spread_probs = theta
        
        # likelihood for hidden Markov model
        if mode == "HMM":
            res = 0
            obs_num = len(self.system["ipsi"].obs_list)
            
            num_time_points = len(time_dists[t_stages[0]])
            ipsi_tmp = np.zeros(shape=(obs_num), dtype=float)
            ipsi_tmp[0] = 1.
            contra_tmp = np.zeros(shape=(obs_num), dtype=float)
            contra_tmp[0] = 1.
            
            # matrices that hold rows of involvement probability for columns of 
            # time-steps
            pXt = {}
            pXt["ipsi"] = self.system["ipsi"]._evolve(t_last=num_time_points-1)
            pXt["contra"] = self.system["contra"]._evolve(t_last=num_time_points-1)
            
            for stage in t_stages:
                PT = np.diag(time_dists[stage])
                
                # This matrix made up of the joint probabilities for all 
                # combinations of ipsi- & contralateral complete diagnoses. Rows 
                # specify ipsi-, columns contralateral 
                pZZ = (
                    self.system["ipsi"].B.T 
                    @ pXt["ipsi"].T 
                    @ PT 
                    @ pXt["contra"] 
                    @ self.system["contra"].B
                )
                
                log_array = np.log(
                    # this sum, as well as the dot product inside, do the 
                    # marginalization over incomplete diagnoses. It's actually 
                    # an algebra trick and the same as tr(C_i @ pZZ @ C_c). We 
                    # need to take the trace to compare the same patients 
                    # on both sides
                    np.sum(
                        self.system["ipsi"].C_dict[stage] 
                        * (pZZ @ self.system["contra"].C_dict[stage]),
                        axis=0
                    )
                )
                # sum up all patients
                res += np.sum(log_array)
                
            return res
        
        elif mode == "BN":
            msg = ("Bayesian network likelihood not yet imlemented.")
            raise ValueError(msg)
    
    
    def combined_likelihood(self, 
                            theta: np.ndarray, 
                            t_stages: List[str] = ["early", "late"],
                            first_p: float = 0.3,
                            T_max: int = 10) -> float:
        """Compute likelihood when the parameters of all T-stage's time-priors 
        (binomial distributions) except the first one are unknown.
        
        Args:
            theta: Set of parameters, consisting of the spread probabilities 
                (as many as the system has :class:`Edge` instances) and the 
                distributions's parameters (one less than the number of 
                T-stages).
                
            t_stages: keywords of T-stages that are present in the dictionary of 
                C matrices and the previously loaded dataset.
                
            first_p: Parameter passed to the Binomial distribution that forms 
                the time-prior of the first T-stage.
                
            T_max: maximum number of time steps.
            
        Returns:
            The combined likelihood of observing patients with different 
            T-stages, given the spread probabilities as well as the parameters 
            for the later (except the first) T-stage's binomial time prior.
        """
        if first_p < 0. or first_p > 1.:
            raise ValueError("first time-prior's parameter must be between "
                             "0 and 1")
        if np.any(np.greater(0., theta)) or np.any(np.greater(theta, 1.)):
            return -np.inf

        add_params = len(t_stages) - 1
        theta, ps = theta[:-add_params], theta[-add_params:]
        t = np.arange(T_max+1)
        pt = lambda p : sp.stats.binom.pmf(t,T_max,p)

        time_dist_dict = {}
        time_dist_dict[t_stages[0]] = pt(first_p)
        for i,p in enumerate(ps):
            time_dist_dict[t_stages[1+i]] = pt(p)
        
        return self.marg_likelihood(theta, t_stages, time_dist_dict, mode="HMM")
    
    
    def risk(self,
             spread_probs: Optional[np.ndarray] = None,
             inv_dict: Dict[str, Optional[np.ndarray]] = {"ipsi": None,
                                                          "contra": None},
             diag_dict: Dict[str, Dict[str, np.ndarray]] = {"ipsi": {}, 
                                                            "contra": {}},
             time_dist: Optional[np.ndarray] = None,
             mode: str = "HMM") -> float:
        """Compute risk of ipsi- & contralateral involvement given specific (but 
        potentially incomplete) diagnoses for each side of the neck.
        
        Args:
            spread_probs: Set of new spread parameters. If not given (``None``), the 
                currently set parameters will be used.
                
            inv_dict: Dictionary that can have the keys ``"ipsi"`` and ``"contra"`` 
                with the respective values being the involvements of interest. 
                If (for one side or both) no involvement of interest is given, 
                it'll be marginalized.
                The array themselves may contain ``True``, ``False`` or ``None`` 
                for each LNL corresponding to the risk for involvement, no 
                involvement and "not interested".
                
            diag_dict: Dictionary that itself may contain two dictionaries. One 
                with key "ipsi" and one with key "contra". The respective value 
                is then a dictionary that can hold a potentially incomplete 
                (mask with ``None``) diagnose for every available modality. 
                Leaving out available modalities will assume a completely 
                missing diagnosis.
                
            time_dist: Prior distribution over time. Must sum to 1 and needs 
                to be given for ``mode = "HMM"``.
                
            mode: Set to ``"HMM"`` for the hidden Markov model risk (requires 
                the ``time_dist``) or to ``"BN"`` for the Bayesian network 
                version.
        """
        # assign spread_probs to system or use the currently set one
        if spread_probs is not None:
            self.spread_probs = spread_probs
            
        cX = {}   # marginalize over matching complete involvements.
        cZ = {}   # marginalize over Z for incomplete diagnoses.
        pXt = {}  # probability p(X|t) of state X at time t as 2D matrices
        pD = {}   # probability p(D|X) of a (potentially incomplete) diagnose, 
                  # given an involvement. Should be a 1D vector
                  
        for side in ["ipsi", "contra"]:            
            inv = np.array(inv_dict[side])
            # build vector to marginalize over involvements
            cX[side] = np.zeros(shape=(len(self.system[side].state_list)),
                                dtype=bool)
            for i,state in enumerate(self.system[side].state_list):
                cX[side][i] = np.all(
                    np.equal(
                        inv, state,
                        where=(inv!=None),
                        out=np.ones_like(inv, dtype=bool)
                    )
                )
                
            # create one large diagnose vector from the individual modalitie's
            # diagnoses
            obs = np.array([])
            for mod in self.system[side]._modality_tables:
                if mod in diag_dict[side]:
                    obs = np.append(obs, diag_dict[side][mod])
                else:
                    obs = np.append(obs, np.array([None] * len(self.lnls)))
            
            # build vector to marginalize over diagnoses
            cZ[side] = np.zeros(shape=(len(self.system[side].obs_list)), 
                                dtype=bool)
            for i,complete_obs in enumerate(self.system[side].obs_list):
                cZ[side][i] = np.all(
                    np.equal(
                        obs, complete_obs,
                        where=(obs!=None), 
                        out=np.ones_like(obs, dtype=bool)
                    )
                )
            
            # compute some (conditional) probability matrices
            num_time_points = len(time_dist)
            pXt[side] = self.system[side]._evolve(t_last=num_time_points-1)
            pD[side] = self.system[side].B @ cZ[side]
        
        # time-prior in diagnoal matrix form
        PT = np.diag(time_dist)

        # joint probability of Xi & Xc, marginalized over time. Acts as prior 
        # for p( Di,Dc | Xi,Xc ) and should be a 2D matrix
        pXX = pXt["ipsi"].T @ PT @ pXt["contra"]
        
        # joint probability of all hidden states and the requested diagnosis
        pDDXX = np.einsum("i,ij,j->ij", pD["ipsi"], pXX, pD["contra"])
        # joint probability of the requested involvement and diagnosis
        pDDII = cX["ipsi"].T @ pDDXX @ cX["contra"]
        
        # denominator p(Di, Dc). Joint probability for ipsi- & contralateral 
        # diagnoses. Marginalized over all hidden involvements and over all 
        # matching complete observations that give rise to the specific 
        # diagnose. The result should be just a number
        pDD = (cZ["ipsi"].T
               @ self.system["ipsi"].B.T
               @ pXX
               @ self.system["contra"].B
               @ cZ["contra"])
        
        return pDDII / pDD
