from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import emcee
import h5py
import numpy as np
import pandas as pd
from scipy.special import factorial as fact

import lymph


def lyprox_to_lymph(
    data: pd.DataFrame,
    method: str = "unilateral",
    modalities: List[str] = ["MRI", "PET"],
    convert_t_stage: Optional[Dict[int, Any]] = None
) -> pd.DataFrame:
    """Convert LyProX output into pandas :class:`DataFrame` that the lymph
    package can use for sampling.

    `LyProX <https://lyprox.org>`_ is our online interface where we make
    detailed patterns of involvement on a per-patient basis available and
    visualize it in useful ways.

    Args:
        data: Patient data exported from the LyProX interface.
        method: Can be ``"unilateral"``, ``"bilateral"`` or ``"midline"``. It
            corresponds to the three lymphatic network classes that are
            implemented in the lymph package.
        modalities: List of diagnostic modalities that should be extracted from
            the exported data.
        convert_t_stage: For each of the possible T-categories (0, 1, 2, 3, 4)
            this dictionary holds a key where the corresponding value is the
            'converted' T-category. For example, if one only wants to
            differentiate between 'early' and 'late', then that dictionary
            would look like this:

            .. code-block:: python

                convert_t_stage = {
                    0: 'early',
                    1: 'early',
                    2: 'early',
                    3: 'late',
                    4: 'late'
                }
    Returns:
        A converted pandas :class:`DataFrame` that can then be used with the
        lymph package.
    """
    t_stage_data = data[("tumor", "1", "t_stage")]
    midline_extension_data = data[("tumor", "1", "extension")]
    diagnostic_data = data[modalities].drop(columns=["date"], level=2)

    if convert_t_stage is not None:
        diagnostic_data[("info", "tumor", "t_stage")] = [
            convert_t_stage[t] for t in t_stage_data.values
        ]
    else:
        diagnostic_data[("info", "tumor", "t_stage")] = t_stage_data

    if method == "midline":
        diagnostic_data[("info", "tumor", "midline_extension")] = midline_extension_data
    elif method == "unilateral":
        diagnostic_data = diagnostic_data.drop(columns=["contra"], level=1)
        diagnostic_data.columns = diagnostic_data.columns.droplevel(1)

    return diagnostic_data


class EnsembleSampler(emcee.EnsembleSampler):
    """A custom wrapper of emcee's ``EnsembleSampler`` that adds a sampling
    method that automatically tracks convergence.
    """
    def __init__(
        self,
        nwalkers,
        ndim,
        log_prob_fn,
        pool=None,
        moves=None,
        args=None,
        kwargs=None,
        backend=None,
        vectorize=False,
        blobs_dtype=None,
        parameter_names: Optional[Union[Dict[str, int], List[str]]] = None
    ):
        """Just define a default mixture of moves.
        """
        if moves is None:
            moves = [
                (emcee.moves.DEMove(),        0.8),
                (emcee.moves.DESnookerMove(), 0.2)
            ]

        super().__init__(
            nwalkers,
            ndim,
            log_prob_fn,
            pool,
            moves,
            args,
            kwargs,
            backend,
            vectorize,
            blobs_dtype,
            parameter_names
        )

    def run_sampling(
        self,
        max_steps: int = 10000,
        check_interval: int = 100,
        trust_threshold: float = 50.,
        rel_acor_threshold: float = 0.05,
        verbose: bool = True,
        **kwargs
    ) -> np.ndarray:
        """Extract ``start`` from settings of the sampler and perform sampling
        while monitoring the convergence.

        Args:
            max_steps: Maximum number of sampling steps to perform.
            check_interval: Number of sampling steps after which to check for
                convergence.
            trust_threshold: The autocorrelation estimate is only trusted when
                it is smaller than the number of samples drawn divided by this
                parameter.
            rel_acor_threshold: The relative change of two consequtive trusted
                autocorrelation estimates must fall below.
            verbose: Show progress during sampling and success at the end.
            **kwargs: Any other ``kwargs`` are directly passed to the ``sample``
                method.

        Returns:
            A list of mean autocorrelation times, computed every
            ``check_interval`` samples.
        """
        if verbose:
            print("Starting sampling")

        start = np.random.uniform(
            low=0., high=1.,
            size=(self.nwalkers, self.ndim)
        )

        acor_list = []
        old_acor = np.inf
        idx = 0
        is_converged = False

        for sample in self.sample(start, iterations=max_steps, progress=verbose, **kwargs):
            # after `check_interval` number of samples...
            if self.iteration % check_interval:
                continue

            # ...compute the autocorrelation time and store it in an array.
            new_acor = self.get_autocorr_time(tol=0)
            acor_list.append(np.mean(new_acor))
            idx += 1

            # check convergence based on two criterions:
            # - has the acor time crossed the N / `trust_theshold` line?
            # - did the acor time stay stable?
            is_converged = np.all(new_acor * trust_threshold < self.iteration)
            rel_acor_diff = np.abs(old_acor - new_acor) / new_acor
            is_converged &= np.all(rel_acor_diff < rel_acor_threshold)

            # if it has converged, stop
            if is_converged:
                break

            old_acor = new_acor

        if verbose:
            if is_converged:
                print(f"Sampler converged after {self.iteration} steps")
            else:
                print("Max. number of steps reached")

            acc_frac = 100 * np.mean(self.acceptance_fraction)
            print(f"Acceptance fraction = {acc_frac:.2f}%")
            print(f"Mean autocorrelation time = {np.mean(old_acor):.2f}")

        return acor_list


def tupledict_to_jsondict(dict: Dict[Tuple[str], List[str]]) -> Dict[str, List[str]]:
    """Take a dictionary that has tuples as keys and stringify those keys so
    that it can be serialized to JSON.
    """
    jsondict = {}
    for k, v in dict.items():
        if np.any([',' in s for s in k]):
            raise ValueError("Strings in in key tuple must not contain commas")

        jsondict[",".join(k)] = v
    return jsondict

def jsondict_to_tupledict(dict: Dict[str, List[str]]) -> Dict[Tuple[str], List[str]]:
    """Take a serialized JSON dictionary where the keys are strings of
    comma-separated names and convert them into keys of tuples.
    """
    tupledict = {}
    for k, v in dict.items():
        tupledict[tuple(n for n in k.split(","))] = v
    return tupledict


class HDFMixin(object):
    """Mixin for the :class:`Unilateral`, :class:`Bilateral` and
    :class:`MidlineBilateral` classes to provide the ability to store and load
    settings to and from an HDF5 file.
    """
    graph: Dict[Tuple[str], List[str]]
    patient_data: pd.DataFrame
    modalities: Dict[str, List[float]]

    def to_hdf(
        self,
        filename: str,
        name: str = "",
    ):
        """Store some important settings as well as the loaded data in the
        specified HDF5 file.

        Args:
            filename: Name of or path to HDF5 file.
            name: Name of the group where the info is supposed to be
                stored.
        """
        filename = Path(filename).resolve()

        with h5py.File(filename, 'a') as file:
            group = file.require_group(f"{name}")
            group.attrs["class"] = self.__class__.__name__
            group.attrs["graph"] = json.dumps(tupledict_to_jsondict(self.graph))
            group.attrs["modalities"] = json.dumps(self.modalities)
            group.attrs["base_symmetric"] = getattr(
                self, "base_symmetric", "None"
            )
            group.attrs["trans_symmetric"] = getattr(
                self, "trans_symmetric", "None"
            )

        with pd.HDFStore(filename, 'a') as store:
            store.put(
                key=f"{name}/patient_data",
                value=self.patient_data,
                format="fixed",     # due to MultiIndex this needs to be fixed
                data_columns=None
            )

def system_from_hdf(
    filename: str,
    name: str = "",
    **kwargs
):
    """Create a lymph system instance from the information saved in an HDF5
    file.

    Args:
        filename: Name of the HDF5 file where the info is stored.
        name: Subgroup where to look for the stored settings and data.

    Any other keyword arguments are passed directly to the constructor of the
    respective class.

    Returns:
        An instance of :class:`lymph.Unilateral`, :class:`lymph.Bilateral` or
        :class:`lymph.MidlineBilateral`.
    """
    filename = Path(filename).resolve()
    recover_None = lambda val: val if val != "None" else None

    with h5py.File(filename, 'a') as file:
        group = file.require_group(f"{name}")
        classname = group.attrs["class"]
        graph = jsondict_to_tupledict(json.loads(group.attrs["graph"]))
        modalities = json.loads(group.attrs["modalities"])
        base_symmetric = recover_None(group.attrs["base_symmetric"])
        trans_symmetric = recover_None(group.attrs["trans_symmetric"])

    with pd.HDFStore(filename, 'a') as store:
        patient_data = store.get(f"{name}/patient_data")

    if classname == "Unilateral":
        new_cls = lymph.Unilateral
    elif classname == "Bilateral":
        new_cls = lymph.Bilateral
    elif classname == "MidlineBilateral":
        new_cls = lymph.MidlineBilateral
    else:
        raise RuntimeError(
            "The classname loaded from the file does not correspond to an "
            "implemented class in the `lymph` package."
        )

    new_sys = new_cls(
        graph=graph,
        base_symmetric=base_symmetric,
        trans_symmetric=trans_symmetric,
        **kwargs
    )
    new_sys.modalities = modalities
    new_sys.patient_data = patient_data
    return new_sys


def fast_binomial_pmf(k: int, n: int, p: float):
    """Compute the probability mass function of the binomial distribution.
    """
    q = (1. - p)
    binom_coeff = fact(n) / (fact(k) * fact(n - k))
    return binom_coeff * p**k * q**(n - k)


def change_base(
    number: int,
    base: int,
    reverse: bool = False,
    length: Optional[int] = None
) -> str:
    """Convert an integer into another base.

    Args:
        number: Number to convert
        base: Base of the resulting converted number
        reverse: If true, the converted number will be printed in reverse order.
        length: Length of the returned string. If longer than would be
            necessary, the output will be padded.

    Returns:
        The (padded) string of the converted number.
    """
    if number < 0:
        raise ValueError("Cannot convert negative numbers")
    if base > 16:
        raise ValueError("Base must be 16 or smaller!")
    elif base < 2:
        raise ValueError("There is no unary number system, base must be > 2")

    convertString = "0123456789ABCDEF"
    result = ''

    if number == 0:
        result += '0'
    else:
        while number >= base:
            result += convertString[number % base]
            number = number//base
        if number > 0:
            result += convertString[number]

    if length is None:
        length = len(result)
    elif length < len(result):
        length = len(result)
        warnings.warn("Length cannot be shorter than converted number.")

    pad = '0' * (length - len(result))

    if reverse:
        return result + pad
    else:
        return pad + result[::-1]


def comp_state_dist(table: np.ndarray) -> Tuple[np.ndarray, List[str]]:
    """Compute the distribution of distinct states/diagnoses from a table of
    individual diagnoses detailing the patterns of lymphatic progression per
    patient.

    Args:
        table: Rows of patients and columns of LNLs, reporting which LNL was
            involved for which patient.

    Returns:
        A histogram of unique states and a list of the corresponding state
        labels.

    Note:
        This function cannot deal with parts of the diagnose being unknown. So
        if, e.g., one level isn't reported for a patient, that row will just be
        ignored.
    """
    _, num_cols = table.shape
    table = table.astype(float)
    state_dist = np.zeros(shape=2**num_cols, dtype=int)
    for row in table:
        if not np.any(np.isnan(row)):
            idx = int(np.sum([n * 2**i for i,n in enumerate(row[::-1])]))
            state_dist[idx] += 1

    state_labels = []
    for i in range(2**num_cols):
        state_labels.append(change_base(i, 2, length=num_cols))

    return state_dist, state_labels


def draw_diagnose_times(
    num_patients: int,
    stage_dist: Dict[Any, float],
    diag_times: Optional[Dict[Any, int]] = None,
    time_dists: Optional[Dict[Any, List[float]]] = None,
) -> Tuple[List[int], List[Any]]:
    """Draw T-stages from a distribution over them and determine the
    corresponding diagnose time or draw a one from a distribution over diagnose
    times defined for the respective T-stage.

    Args:
        num_patients: Number of patients to draw diagnose times for.
        stage_dist: Distribution over T-stages.
        diag_times: Fixed diagnose time for a given T-stage.
        time_dists: Holds a distribution over diagnose times for each T-stage
            from which the diagnose times will be drawn if it is given. If this
            is ``None``, ``diag_times`` must be provided.

    Returns:
        The drawn T-stages as well as the drawn diagnose times.
    """
    if num_patients < 1:
        raise ValueError("Number of patients to draw must be 1 or larger")
    if not np.isclose(np.sum(stage_dist), 1.):
        raise ValueError("Distribution over T-stages must sum to 1.")

    # draw the diagnose times for each patient
    if diag_times is not None:
        t_stages = list(diag_times.keys())
        drawn_t_stages = np.random.choice(
            t_stages,
            p=stage_dist,
            size=num_patients
        )
        drawn_diag_times = [diag_times[t] for t in drawn_t_stages]

    elif time_dists is not None:
        t_stages = list(time_dists.keys())
        max_t = len(time_dists[t_stages[0]]) - 1
        time_steps = np.arange(max_t + 1)

        drawn_t_stages = np.random.choice(
            t_stages,
            p=stage_dist,
            size=num_patients
        )
        drawn_diag_times = [
            np.random.choice(time_steps, p=time_dists[t])
            for t in drawn_t_stages
        ]

    else:
        raise ValueError(
            "Either `diag_times`or `time_dists` must be provided"
        )

    return drawn_t_stages, drawn_diag_times


def draw_from_simplex(ndim: int, nsample: int = 1) -> np.ndarray:
    """Draw uniformly from an n-dimensional simplex.

    Args:
        ndim: Dimensionality of simplex to draw from.
        nsample: Number of samples to draw from the simplex.

    Returns:
        A matrix of shape (nsample, ndim) that sums to one along axis 1.
    """
    if ndim < 1:
        raise ValueError("Cannot generate less than 1D samples")
    if nsample < 1:
        raise ValueError("Generating less than one sample doesn't make sense")

    rand = np.random.uniform(size=(nsample, ndim-1))
    unsorted = np.concatenate(
        [np.zeros(shape=(nsample,1)), rand, np.ones(shape=(nsample,1))],
        axis=1
    )
    sorted = np.sort(unsorted, axis=1)

    diff_arr = np.concatenate([[-1., 1.], np.zeros(ndim-1)])
    diff_mat = np.array([np.roll(diff_arr, i) for i in range(ndim)]).T
    res = sorted @ diff_mat

    return res