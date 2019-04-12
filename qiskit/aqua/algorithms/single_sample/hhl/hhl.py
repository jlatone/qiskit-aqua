# -*- coding: utf-8 -*-

# Copyright 2018 IBM.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =============================================================================
"""
The HHL algorithm.
"""

import logging
import numpy as np
from copy import deepcopy

from qiskit import QuantumRegister, ClassicalRegister, QuantumCircuit
from qiskit.aqua.algorithms import QuantumAlgorithm
from qiskit.aqua import AquaError, Pluggable, PluggableType, get_pluggable_class
from qiskit.ignis.verification.tomography import state_tomography_circuits, \
    StateTomographyFitter
from qiskit.converters import circuit_to_dag

logger = logging.getLogger(__name__)


class HHL(QuantumAlgorithm):

    """The HHL algorithm.

    The quantum circuit for this algorithm is returned by `generate_circuit`.
    Running the algorithm will execute the circuit and return the result
    vector, measured (real hardware backend) or derived (qasm_simulator) via
    state tomography or calculated from the statevector (statevector_simulator).
    """

    CONFIGURATION = {
        'name': 'HHL',
        'description': 'The HHL Algorithm for Solving Linear Systems of '
                       'equations',
        'input_schema': {
            '$schema': 'http://json-schema.org/schema#',
            'id': 'hhl_schema',
            'type': 'object',
            'properties': {
                'auto_hermitian': {
                    'type': 'boolean',
                    'default': False
                },
                'auto_resize': {
                    'type': 'boolean',
                    'default': False
                }
            },
            'additionalProperties': False
        },
        'problems': ['linear_system'],
        'depends': [
            {'pluggable_type': 'initial_state',
             'default': {
                     'name': 'CUSTOM',
                }
             },
            {'pluggable_type': 'eigs',
             'default': {
                     'name': 'EigsQPE',
                     'num_ancillae': 6,
                     'num_time_slices': 50,
                     'expansion_mode': 'suzuki',
                     'expansion_order': 2
                }
             },
            {'pluggable_type': 'reciprocal',
             'default': {
                     'name': 'Lookup'
                }
             }
        ],
    }

    def __init__(
            self,
            matrix=None,
            vector=None,
            auto_hermitian=False,
            auto_resize=False,
            eigs=None,
            init_state=None,
            reciprocal=None,
            num_q=0,
            num_a=0,
            orig_size=0
    ):
        """
        Constructor.

        Args:
            matrix (np.array): the input matrix of linear system of equations
            vector (np.array): the input vector of linear system of equations
            auto_hermitian (bool): flag indicating automatic expansion of a non-hermitian matrix
            auto_resize (bool): flag indicating automatic expansion to 2**n dimensional matrix
            eigs (Eigenvalues): the eigenvalue estimation instance
            init_state (InitialState): the initial quantum state preparation
            reciprocal (Reciprocal): the eigenvalue reciprocal and controlled rotation instance
            num_q (int): number of qubits required for the matrix Operator instance
            num_a (int): number of ancillary qubits for Eigenvalues instance
            orig_size (int): The original dimension of the problem (if auto_hermitian OR auto_resize)
        """
        super().__init__()
        super().validate(locals())
        self._matrix = matrix
        self._vector = vector
        self._auto_hermitian = auto_hermitian
        self._auto_resize = auto_resize
        self._eigs = eigs
        self._init_state = init_state
        self._reciprocal = reciprocal
        self._num_q = num_q
        self._num_a = num_a
        self._circuit = None
        self._io_register = None
        self._eigenvalue_register = None
        self._ancilla_register = None
        self._success_bit = None
        self._original_dimension = orig_size
        self._ret = {}

    @classmethod
    def init_params(cls, params, algo_input):
        """Initialize via parameters dictionary and algorithm input instance

        Args:
            params: parameters dictionary
            algo_input: LinearSystemInput instance
        """
        if algo_input is None:
            raise AquaError("LinearSystemInput instance is required.")

        matrix = algo_input.matrix
        vector = algo_input.vector
        if not isinstance(matrix, np.ndarray):
            matrix = np.asarray(matrix)
        if not isinstance(vector, np.ndarray):
            vector = np.asarray(vector)

        if matrix.shape[0] != len(vector):
            raise ValueError("Input vector dimension does not match input "
                             "matrix dimension!")
        if matrix.shape[0] != matrix.shape[1]:
            raise ValueError("Input matrix must be square!")

        hhl_params = params.get(Pluggable.SECTION_KEY_ALGORITHM)
        auto_hermitian = hhl_params.get('auto_hermitian')
        auto_resize = hhl_params.get('auto_resize')
        orig_size = len(vector)

        is_hermitian = np.allclose(matrix, np.matrix(matrix).H)
        is_correctsize = np.log2(matrix.shape[0]) % 1 == 0

        if auto_hermitian and not is_hermitian:
            # convert a non-hermitian matrix A to a hermitian matrix
            # by [[0, A.H], [A, 0]] and expand vector b to [b.conj, b]
            half_dim = matrix.shape[0]
            full_dim = 2 * half_dim
            new_matrix = np.zeros([full_dim, full_dim])
            new_matrix = np.array(new_matrix, dtype=complex)
            new_matrix[0:half_dim, half_dim:full_dim] = matrix[:, :]
            new_matrix[half_dim:full_dim, 0:half_dim] = np.matrix(matrix).H[:, :]
            matrix = new_matrix
            new_vector = np.zeros((1, full_dim))
            new_vector = np.array(new_vector, dtype=complex)
            new_vector[0, :vector.shape[0]] = vector.conj()
            new_vector[0, vector.shape[0]:] = vector
            vector = new_vector.reshape(np.shape(new_vector)[1])

        if auto_resize and not is_correctsize:
            # extend vector and matrix for non 2**n dimensional matrices
            mat_dim = matrix.shape[0]
            next_higher = int(np.ceil(np.log2(mat_dim)))
            new_matrix = np.identity(2 ** next_higher)
            new_matrix = np.array(new_matrix, dtype=complex)
            new_matrix[:mat_dim, :mat_dim] = matrix[:, :]
            matrix = new_matrix
            new_vector = np.zeros((1, 2 ** next_higher))
            new_vector[0, :vector.shape[0]] = vector
            vector = new_vector.reshape(np.shape(new_vector)[1])

        if not np.allclose(matrix, np.matrix(matrix).H):
            raise ValueError("Input matrix is not hermitian!")
        if np.log2(matrix.shape[0]) % 1 != 0:
            raise ValueError("Matrix dimension must be 2**n!")

        logger.debug(f"matrix {np.round(matrix, 3)}")
        logger.debug(f"vector {np.round(vector, 3)}")
        logger.debug(f"Original dimension recorded as {orig_size}")
        logger.debug(f"Current dimension of Matrix: {str(matrix.shape)}")
        logger.debug(f"Current dimension of Vector: {str(vector.shape)}")

        # Initialize eigenvalue finding module
        eigs_params = params.get(Pluggable.SECTION_KEY_EIGS)
        eigs = get_pluggable_class(PluggableType.EIGENVALUES,
                                   eigs_params['name']).init_params(params, matrix)
        num_q, num_a = eigs.get_register_sizes()

        # Initialize initial state module
        tmpvec = vector
        init_state_params = params.get(Pluggable.SECTION_KEY_INITIAL_STATE)
        init_state_params["num_qubits"] = num_q
        init_state_params["state_vector"] = tmpvec
        init_state = get_pluggable_class(PluggableType.INITIAL_STATE,
                                         init_state_params['name']).init_params(params)

        # Initialize reciprocal rotation module
        reciprocal_params = params.get(Pluggable.SECTION_KEY_RECIPROCAL)
        reciprocal_params["negative_evals"] = eigs._negative_evals
        reciprocal_params["evo_time"] = eigs._evo_time
        reci = get_pluggable_class(PluggableType.RECIPROCAL,
                                   reciprocal_params['name']).init_params(params)

        return cls(matrix, vector, auto_hermitian, auto_resize, eigs,
                   init_state, reci, num_q, num_a, orig_size)

    def construct_circuit(self, measurement=False):
        """Construct the HHL circuit.

        Args:
            measurement (bool): indicate whether measurement on ancillary qubit
                should be performed

        Returns:
            the QuantumCircuit object for the constructed circuit
        """

        q = QuantumRegister(self._num_q, name="io")
        qc = QuantumCircuit(q)

        # InitialState
        qc += self._init_state.construct_circuit("circuit", q)

        # EigenvalueEstimation (QPE)
        qc += self._eigs.construct_circuit("circuit", q)
        a = self._eigs._output_register

        # Reciprocal calculation with rotation
        qc += self._reciprocal.construct_circuit("circuit", a)
        s = self._reciprocal._anc

        # Inverse EigenvalueEstimation
        qc += self._eigs.construct_inverse("circuit", self._eigs._circuit,
                                           self._eigs._input_register,
                                           self._eigs._output_register)

        # Measurement of the ancilla qubit
        if measurement:
            c = ClassicalRegister(1)
            qc.add_register(c)
            qc.measure(s, c)
            self._success_bit = c

        self._io_register = q
        self._eigenvalue_register = a
        self._ancilla_register = s
        self._circuit = qc
        return qc

    def _resize_vector(self, vec):
        return vec[:self._original_dimension]

    def _resize_matrix(self):
        new_matrix = np.ndarray(shape=(self._original_dimension, self._original_dimension), dtype=complex)
        new_matrix[:,:] = self._matrix[:self._original_dimension, :self._original_dimension]
        return new_matrix

    def _statevector_simulation(self):
        """The statevector simulation.

        The HHL result gets extracted from the statevector. Only for
        statevector simulator available.
        """
        res = self._quantum_instance.execute(self._circuit)
        sv = np.asarray(res.get_statevector(self._circuit))
        # Extract solution vector from statevector
        vec = self._reciprocal.sv_to_resvec(sv, self._num_q)
        # remove added dimensions
        self._ret['probability_result'] = np.real(self._resize_vector(vec).dot(self._resize_vector(vec).conj()))
        vec = vec/np.linalg.norm(vec)
        self._hhl_results(vec)

    def _state_tomography(self):
        """The state tomography.

        The HHL result gets extracted via state tomography. Available for
        qasm simulator and real hardware backends.
        """

        # Preparing the state tomography circuits
        tomo_circuits = state_tomography_circuits(self._circuit,
                                                  self._io_register)
        tomo_circuits_noanc = deepcopy(tomo_circuits)
        ca = ClassicalRegister(1)
        for circ in tomo_circuits:
            circ.add_register(ca)
            circ.measure(self._reciprocal._anc, ca[0])

        # Extracting the probability of successful run
        results = self._quantum_instance.execute(tomo_circuits)
        probs = []
        for circ in tomo_circuits:
            counts = results.get_counts(circ)
            s, f = 0, 0
            for k, v in counts.items():
                if k[0] == "1":
                    s += v
                else:
                    f += v
            probs.append(s/(f+s))
        probs = self._resize_vector(probs)
        self._ret["probability_result"] = np.real(probs)

        # Filtering the tomo data for valid results with ancillary measured
        # to 1, i.e. c1==1
        results_noanc = self._tomo_postselect(results)
        tomo_data = StateTomographyFitter(results_noanc, tomo_circuits_noanc)
        rho_fit = tomo_data.fit()
        vec = np.diag(rho_fit) / np.sqrt(sum(np.diag(rho_fit) ** 2))
        self._hhl_results(vec)

    def _tomo_postselect(self, results):
        new_results = deepcopy(results)

        for resultidx, _ in enumerate(results.results):
            old_counts = results.get_counts(resultidx)
            new_counts = {}

            # change the size of the classical register
            new_results.results[resultidx].header.creg_sizes = [
                new_results.results[resultidx].header.creg_sizes[0]]
            new_results.results[resultidx].header.clbit_labels = \
                new_results.results[resultidx].header.clbit_labels[0:-1]
            new_results.results[resultidx].header.memory_slots = \
                new_results.results[resultidx].header.memory_slots - 1

            for reg_key in old_counts:
                reg_bits = reg_key.split(' ')
                if reg_bits[0] == '1':
                    new_counts[reg_bits[1]] = old_counts[reg_key]

            new_results.results[resultidx].data.counts = \
                new_results.results[resultidx]. \
                data.counts.from_dict(new_counts)

        return new_results

    def _hhl_results(self, vec):
        logger.debug(f"[statevector_simulation] - Vector pre-resizing {str(vec)}")
        res_vec = self._resize_vector(vec)
        in_vec = self._resize_vector(self._vector)
        logger.debug(f"[statevector_simulation] - Vector post-resizing {str(res_vec)}")
        matrix = self._resize_matrix()
        self._ret["output"] = res_vec
        # Rescaling the output vector to the real solution vector
        tmp_vec = matrix.dot(res_vec)
        f1 = np.linalg.norm(in_vec)/np.linalg.norm(tmp_vec)
        # TODO: unsure about scaling by num_q here. Likely too big for the
        # truncated vector. Alternative: scale by log(original matrix dimension?)
        # f2 = sum(np.angle(in_vec*tmp_vec.conj()-1+1))/self._num_q  # "-1+1" to fix angle error for -0.-0.j
        f2 = sum(np.angle(in_vec*tmp_vec.conj()-1+1))/(np.log2(matrix.shape[0]))
        self._ret["solution"] = f1*res_vec*np.exp(-1j*f2)

    def _run(self):
        if self._quantum_instance.is_statevector:
            self.construct_circuit(measurement=False)
            self._statevector_simulation()
        else:
            self.construct_circuit(measurement=False)
            self._state_tomography()
        # Adding a bit of general result information
        self._ret["matrix"] = self._resize_matrix()
        self._ret["vector"] = self._resize_vector(self._vector)
        self._ret["circuit_info"] = circuit_to_dag(self._circuit).properties()
        return self._ret
