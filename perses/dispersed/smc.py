import simtk.openmm as openmm
import openmmtools.cache as cache
from typing import List, Tuple, Union, NamedTuple
import os
import copy
import openmmtools.cache as cache
from perses.dispersed.utils import *

import openmmtools.mcmc as mcmc
import openmmtools.integrators as integrators
import openmmtools.states as states
from openmmtools.states import ThermodynamicState, CompoundThermodynamicState, SamplerState
import numpy as np
import mdtraj as md
from perses.annihilation.relative import HybridTopologyFactory
import mdtraj.utils as mdtrajutils
import pickle
import simtk.unit as unit
import tqdm
from perses.tests.utils import compute_potential_components
from openmmtools.constants import kB
import pdb
import logging
import tqdm
from sys import getsizeof
import time
from collections import namedtuple
from perses.annihilation.lambda_protocol import LambdaProtocol
from perses.annihilation.lambda_protocol import RelativeAlchemicalState, LambdaProtocol
from perses.dispersed import *
import random
import pymbar
import dask.distributed as distributed
from perses.dispersed.parallel import Parallelism
import tqdm
import time
# Instantiate logger
logging.basicConfig(level = logging.NOTSET)
_logger = logging.getLogger("sMC")
_logger.setLevel(logging.DEBUG)

#cache.global_context_cache.platform = openmm.Platform.getPlatformByName('Reference') #this is just a local version
EquilibriumFEPTask = namedtuple('EquilibriumInput', ['sampler_state', 'inputs', 'outputs'])

class SequentialMonteCarlo():
    """
    This class represents an sMC particle that runs a nonequilibrium switching protocol.
    It is a batteries-included engine for conducting sequential Monte Carlo sampling.

    WARNING: take care in writing trajectory file as saving positions to memory is costly.  Either do not write the configuration or save sparse positions.
    """

    supported_resampling_methods = {'multinomial': multinomial_resample}
    supported_observables = {'ESS': ESS, 'CESS': CESS}

    def __init__(self,
                 factory,
                 lambda_protocol = 'default',
                 temperature = 300 * unit.kelvin,
                 trajectory_directory = 'test',
                 trajectory_prefix = 'out',
                 atom_selection = 'not water',
                 timestep = 1 * unit.femtoseconds,
                 collision_rate = 1 / unit.picoseconds,
                 eq_splitting_string = 'V R O R V',
                 neq_splitting_string = 'V R O R V',
                 ncmc_save_interval = None,
                 measure_shadow_work = False,
                 neq_integrator = 'langevin',
                 external_parallelism = None,
                 internal_parallelism = {'library': ('dask', 'LSF'),
                                         'num_processes': 2}
                                         ):
        """
        Parameters
        ----------
        factory : perses.annihilation.relative.HybridTopologyFactory - compatible object
        lambda_protocol : str, default 'default'
            the flavor of scalar lambda protocol used to control electrostatic, steric, and valence lambdas
        temperature : float unit.Quantity
            Temperature at which to perform the simulation, default 300K
        trajectory_directory : str, default 'test'
            Where to write out trajectories resulting from the calculation. If None, no writing is done.
        trajectory_prefix : str, default None
            What prefix to use for this calculation's trajectory files. If none, no writing is done.
        atom_selection : str, default not water
            MDTraj selection syntax for which atomic coordinates to save in the trajectories. Default strips
            all water.
        timestep : float unit.Quantity, default 1 * units.femtoseconds
            the timestep for running MD
        collision_rate : float unit.Quantity, default 1 / unit.picoseconds
            the collision rate for running MD
        eq_splitting_string : str, default 'V R O R V'
            The integrator splitting to use for equilibrium simulation
        neq_splitting_string : str, default 'V R O R V'
            The integrator splitting to use for nonequilibrium switching simulation
        ncmc_save_interval : int, default None
            interval with which to write ncmc trajectory.  If None, trajectory will not be saved.
            We will assert that the n_lambdas % ncmc_save_interval = 0; otherwise, the protocol will not be complete
        measure_shadow_work : bool, default False
            whether to measure the shadow work of the integrator.
            WARNING : this is not currently supported
        neq_integrator : str, default 'langevin'
            which integrator to use
        external_parallelism : dict('parallelism': perses.dispersed.parallel.Parallelism, 'available_workers': list(str)), default None
            an external parallelism dictionary
        internal_parallelism : dict, default {'library': ('dask', 'LSF'), 'num_processes': 2}
            dictionary of parameters to instantiate a client and run parallel computation internally.  internal parallelization is handled by default
            if None, external worker arguments have to be specified, otherwise, no parallel computation will be conducted, and annealing will be conducted locally.
        """
        _logger.info(f"Initializing SequentialMonteCarlo")

        #pull necessary attributes from factory
        self.factory = factory

        #context cache
        self.context_cache = cache.global_context_cache

        #use default protocol
        self.lambda_protocol = lambda_protocol

        #handle both eq and neq parameters
        self.temperature = temperature
        self.timestep = timestep
        self.collision_rate = collision_rate

        self.measure_shadow_work = measure_shadow_work
        self.neq_integrator = neq_integrator
        if measure_shadow_work:
            raise Exception(f"measure_shadow_work is not currently supported.  Aborting!")


        #handle equilibrium parameters
        self.eq_splitting_string = eq_splitting_string

        #handle storage and names
        self.trajectory_directory = trajectory_directory
        self.trajectory_prefix = trajectory_prefix
        self.atom_selection = atom_selection

        #handle neq parameters
        self.neq_splitting_string = neq_splitting_string
        self.ncmc_save_interval = ncmc_save_interval

        #lambda states:
        self.lambda_endstates = {'forward': [0.0,1.0], 'reverse': [1.0, 0.0]}

        #instantiate trajectory filenames
        if self.trajectory_directory and self.trajectory_prefix:
            self.write_traj = True
            self.eq_trajectory_filename = {lambda_state: os.path.join(os.getcwd(), self.trajectory_directory, f"{self.trajectory_prefix}.eq.lambda_{lambda_state}.h5") for lambda_state in self.lambda_endstates['forward']}
            self.neq_traj_filename = {direct: os.path.join(os.getcwd(), self.trajectory_directory, f"{self.trajectory_prefix}.neq.lambda_{direct}") for direct in self.lambda_endstates.keys()}
            self.topology = self.factory.hybrid_topology
        else:
            self.write_traj = False
            self.eq_trajectory_filename = {0: None, 1: None}
            self.neq_traj_filename = {'forward': None, 'reverse': None}
            self.topology = None

        # subset the topology appropriately:
        self.atom_selection_string = atom_selection
        # subset the topology appropriately:
        if self.atom_selection_string is not None:
            atom_selection_indices = self.factory.hybrid_topology.select(self.atom_selection_string)
            self.atom_selection_indices = atom_selection_indices
        else:
            self.atom_selection_indices = None

        # instantiating equilibrium file/rp collection dicts
        self._eq_dict = {0: [], 1: [], '0_decorrelated': None, '1_decorrelated': None, '0_reduced_potentials': [], '1_reduced_potentials': []}
        self._eq_files_dict = {0: [], 1: []}
        self._eq_timers = {0: [], 1: []}
        self._neq_timers = {'forward': [], 'reverse': []}

        #instantiate nonequilibrium work dicts: the keys indicate from which equilibrium thermodynamic state the neq_switching is conducted FROM (as opposed to TO)
        self.cumulative_work = {'forward': [], 'reverse': []}
        self.incremental_work = copy.deepcopy(self.cumulative_work)
        self.shadow_work = copy.deepcopy(self.cumulative_work)
        self.nonequilibrium_timers = copy.deepcopy(self.cumulative_work)
        self.total_jobs = 0
        #self.failures = copy.deepcopy(self.cumulative_work)
        self.dg_EXP = copy.deepcopy(self.cumulative_work)
        self.dg_BAR = None


        # create an empty dict of starting and ending sampler_states
        self.start_sampler_states = {_direction: [] for _direction in ['forward', 'reverse']}
        self.end_sampler_states = {_direction: [] for _direction in ['forward', 'reverse']}

        #instantiate thermodynamic state
        lambda_alchemical_state = RelativeAlchemicalState.from_system(self.factory.hybrid_system)
        lambda_alchemical_state.set_alchemical_parameters(0.0, LambdaProtocol(functions = self.lambda_protocol))
        self.thermodynamic_state = CompoundThermodynamicState(ThermodynamicState(self.factory.hybrid_system, temperature = self.temperature),composable_states = [lambda_alchemical_state])

        # set the SamplerState for the lambda 0 and 1 equilibrium simulations
        sampler_state = SamplerState(self.factory.hybrid_positions,
                                          box_vectors=self.factory.hybrid_system.getDefaultPeriodicBoxVectors())
        self.sampler_states = {0: copy.deepcopy(sampler_state), 1: copy.deepcopy(sampler_state)}

        #parallelism implementables
        if external_parallelism is not None and internal_parallelism is not None:
            raise Exception(f"external parallelism were given, but an internal parallelization scheme was also specified.  Aborting!")
        if external_parallelism is not None:
            self.external_parallelism, self.internal_parallelism = True, False
            self.parallelism, self.workers = external_parallelism['parallelism'], external_parallelism['workers']
            self.parallelism_parameters = None
            assert self.parallelism.client is not None, f"the external parallelism class has not yet an activated client."
        elif internal_parallelism is not None:
            self.external_parallelism, self.internal_parallelism = False, True
            self.parallelism, self.workers = Parallelism(), internal_parallelism['num_processes']
            self.parallelism_parameters = internal_parallelism
        else:
            _logger.warning(f"both internal and external parallelisms are unspecified.  Defaulting to not_parallel.")
            self.external_parallelism, self.internal_parallelism = False, True
            self.parallelism_parameters = {'library': None, num_processes = 0}
            self.parallelism, self.workers = Parallelism(), 0

        if external_parallelism is not None and internal_parallelism is not None:
            raise Exception(f"external parallelism were given, but an internal parallelization scheme was also specified.  Aborting!")

    def _activate_annealing_workers(self):
        """
        wrapper to distribute workers and create appropriate worker attributes for annealing
        """
        if self.internal_parallelism:
            #we have to activate the client
            self.parallelism.activate_client(library = self.parallelism_parameters['library'],
                                             num_processes = len(self.parallelism_parameters['num_processes']))
            workers = list(self.parallelism.workers.values())
        elif self.external_parallelism:
            #the client is already active
            workers = self.parallelism_parameters['available_workers']
        else:
            raise Exception(f"either internal or external parallelism must be True.")

        #now client.run to broadcast the vars
        _remote_worker = True if self.parallelism.client is not None else False
        _thermodynamic_state = self.thermodynamic_state,
        _lambda_protocol = self.lambda_protocol,
        _timestep = self.timestep,
        _collision_rate = self.collision_rate,
        _temperature = self.temperature,
        _neq_splitting_string = self.neq_splitting_string,
        _ncmc_save_interval = self.ncmc_save_interval,
        _topology = self.topology,
        _subset_atoms = self.subset_atoms,
        _measure_shadow_work = self.measure_shadow_work,
        _integrator = self.neq_integrator

        addresses = self.parallelism.run_all(activate_LocallyOptimalAnnealing, #func
                                             _remote_worker, #arg: remote worker
                                             _thermodynamic_state, #arg: thermodynamic state
                                             _lambda_protocol, #arg: lambda protocol
                                             _timestep, #arg: timestep
                                             _collision_rate, #arg: collision_rate
                                             _temperature, #arg: temperature
                                             _neq_splitting_string, #arg: neq_splitting string
                                             _ncmc_save_interval, #arg: ncmc_save_interval
                                             _topology, #arg: topology
                                             _subset_atoms, #arg: subset atoms
                                             _measure_shadow_work, #arg: measure_shadow_work
                                             _integrator, #arg: integrator
                                             workers = workers) #workers
    def _deactivate_annealing_workers(self):
        """
        wrapper to deactivate workers and delete appropriate worker attributes for annealing
        """
        if self.internal_parallelism:
            #we have to deactivate the client
            self.parallelism.deactivate_client()
        elif self.external_parallelism:
            #the client is already active; we don't have the authority to deactivate
            workers = self.parallelism_parameters['available_workers']
            _remote_worker = True if self.parallelism.client is not None else False
            deactivate_worker_attributes(remote_worker = _remote_worker)
        else:
            raise Exception(f"either internal or external parallelism must be True.")

    # def AIS(self,
    #         num_particles,
    #         protocol_length,
    #         directions = ['forward','reverse'],
    #         num_integration_steps = 1,
    #         return_timer = False,
    #         rethermalize = False):
    #     """
    #     Conduct vanilla AIS. with a linearly interpolated lambda protocol
    #
    #     Arguments
    #     ----------
    #     num_particles : int
    #         number of particles to run in each direction
    #     protocol_length : int
    #         number of lambdas
    #     directions : list of str, default ['forward', 'reverse']
    #         the directions to run.
    #     num_integration_steps : int
    #         number of integration steps per proposal
    #     return_timer : bool, default False
    #         whether to time the annealing protocol
    #     rethermalize : bool, default False
    #         whether to rethermalize velocities after proposal
    #     """
    #     self.activate_client(LSF = self.LSF,
    #                         num_processes = self.num_processes,
    #                         adapt = self.adapt)
    #
    #     for _direction in directions:
    #         assert _direction in ['forward', 'reverse'], f"direction {_direction} is not an appropriate direction"
    #     protocols = {}
    #     for _direction in directions:
    #         if _direction == 'forward':
    #             protocol = np.linspace(0, 1, protocol_length)
    #         elif _direction == 'reverse':
    #             protocol = np.linspace(1, 0, protocol_length)
    #         protocols.update({_direction: protocol})
    #
    #     num_actors, num_actors_per_direction, num_particles_per_actor, AIS_actors = self._actor_distribution(directions = directions,
    #                                                                                                          num_particles = num_particles)
    #     AIS_actor_futures = {_direction: [[[None] * num_particles_per_actor] for _ in range(num_actors_per_direction)]}
    #     AIS_actor_results = copy.deepcopy(AIS_actor_futures)
    #
    #     for job in tqdm.trange(num_particles_per_actor):
    #         start_timer = time.time()
    #         for _direction in directions:
    #             _logger.info(f"entering {_direction} direction to launch annealing jobs.")
    #             for _actor in range(num_actors_per_direction):
    #                 _logger.info(f"\tretrieving actor {_actor}.")
    #                 sampler_state = self.pull_trajectory_snapshot(0) if _direction == 'forward' else self.pull_trajectory_snapshot(1)
    #                 if self.ncmc_save_interval is not None: #check if we should make 'trajectory_filename' not None
    #                     noneq_trajectory_filename = self.neq_traj_filename[_direction] + f".iteration_{self.total_jobs:04}.h5"
    #                     self.total_jobs += 1
    #                 else:
    #                     noneq_trajectory_filename = None
    #
    #                 actor_future = AIS_actors[_direction][_actor].anneal(sampler_state = sampler_state,
    #                                                                      lambdas = protocols[_direction],
    #                                                                      label = self.total_jobs,
    #                                                                      noneq_trajectory_filename = noneq_trajectory_filename,
    #                                                                      num_integration_steps = num_integration_steps,
    #                                                                      return_timer = return_timer,
    #                                                                      return_sampler_state = False,
    #                                                                      rethermalize = rethermalize,
    #                                                                      )
    #                 AIS_actor_futures[_direction][_actor][job] = actor_future
    #
    #
    #         #now we collect the jobs
    #         for _direction in directions:
    #             for _actor in range(num_actors_per_direction):
    #                 _future = AIS_actors_futures[_direction][_actor][job]
    #                 result = self.gather_actor_result(_future)
    #                 AIS_actor_results[_direction][_actor][job] = result
    #
    #         end_timer = time.time() - start_timer
    #         _logger.info(f"iteration took {end_timer} seconds.")
    #
    #
    #     #now that the actors are gathered, we can collect the results and put them into class attributes
    #     _logger.info(f"organizing all results...")
    #     for _direction in AIS_actor_results.keys():
    #         _result_lst = AIS_actor_results[_direction]
    #         _logger.info(f"collecting {_direction} actor results...")
    #         flattened_result_lst = [item for sublist in _result_lst for item in sublist]
    #         [self.incremental_work[_direction].append(item[0]) for item in flattened_result_lst]
    #         [self.nonequilibrium_timers[_direction].append(item[2]) for item in flattened_result_lst]
    #
    #     #compute the free energy
    #     self.compute_free_energy()
    #
    #     #deactivate_client
    #     self.deactivate_client()

    def sMC_anneal(self,
                   num_particles,
                   protocols = {'forward': np.linspace(0,1, 1000), 'reverse': np.linspace(1,0,1000)},
                   directions = ['forward','reverse'],
                   num_integration_steps = 1,
                   return_timer = False,
                   rethermalize = False,
                   trailblaze = None,
                   resample = None):
        """
        Conduct generalized sMC annealing with trailblazing functionality.

        Arguments
        ----------
        num_particles : int
            number of particles to run in each direction
        protocols : dict of {direction : np.array}, default {'forward': np.linspace(0,1, 1000), 'reverse': np.linspace(1,0,1000)},
            the dictionary of forward and reverse protocols.  if None, the protocols will be trailblazed.
        directions : list of str, default ['forward', 'reverse']
            the directions to run.
        num_integration_steps : int
            number of integration steps per proposal
        return_timer : bool, default False
            whether to time the annealing protocol
        rethermalize : bool, default False
            whether to rethermalize velocities after proposal
        trailblaze : dict, default None
            which observable/criterion to use for trailblazing and the threshold
            if None, trailblazing is not conducted;
            else: the dict must have the following format:
                {'criterion': str, 'threshold': float}
        resample : dict, default None
            the resample dict specifies the resampling criterion and threshold, as well as the resampling method used.  if None, no resampling is conduced;
            otherwise, the resample dict must take the following form:
            {'criterion': str, 'method': str, 'threshold': float}
        """
        #first some bookkeeping/validation
        for _direction in directions:
            assert _direction in ['forward', 'reverse'], f"direction {_direction} is not an appropriate direction"

        if protocols is not None:
            assert set(protocols.keys()) == set(directions), f"protocols are specified, but do not match the directions in the specified directions"
            if trailblaze is not None:
                raise Exception(f"the protocols were specified, as was the trailblaze criterion.  Only one can be defined")
            _trailblaze = False
            starting_lines = {_direction: protocols[_direction][0] for _direction in directions}
            finish_lines = {_direction: protocols[_direction][-1] for _direction in directions}
            self.protocols = protocols
        else:
            assert trailblaze is not None, f"the protocols weren't specified, and neither was the trailblaze criterion; one must be specified"
            assert set(list(trailblaze.keys())).issubset(set(['criterion', 'threshold'])), f"trailblaze does not contain 'criterion' and 'threshold'"
            _trailblaze = True
            starting_lines, finish_lines = {}, {}
            if 'forward' in directions:
                finish_lines['forward'] = 1.0
                starting_lines['forward'] = 0.0
            if 'reverse' in directions:
                finish_lines['reverse'] = 0.0
                starting_lines['reverse'] = 1.0
            self.protocols = {_direction : [starting_lines[_direction]] for _direction in directions}


        if resample is not None:
            assert resample['criterion'] in list(self.supported_observables.keys()), f"the specified resampling criterion is not supported."
            assert resample['method'] in list(self.supported_resampling_methods), f"the specified resampling method is not supported."
            _resample = True
        else:
            _resample = False

        for _direction in directions:
            assert _direction in ['forward', 'reverse'], f"direction {_direction} is not an appropriate direction"

        if self.internal_parallelism:
            workers = list(self.parallelism.workers.values())
        elif self.external_parallelism:
            workers = self.parallelism_parameters['available_workers']

        remote_worker = True if self.parallelism.client is not None else False


        #initialize recording lists
        _logger.info(f"initializing organizing dictionaries...")

        self._activate_annealing_workers()

        sMC_futures = {_direction: None for _direction in directions}
        _logger.debug(f"\tsMC_futures: {sMC_futures}")

        sMC_sampler_states = {_direction: np.array([self.pull_trajectory_snapshot(int(starting_lines[_direction])) for _ in range(num_particles)] for _direction in directions}
        _logger.debug(f"\tsMC_sampler_states: {sMC_sampler_states}")

        sMC_timers = {_direction: [] for _direction in directions}
        _logger.debug(f"sMC_timers: {sMC_timers}")

        sMC_particle_ancestries = {_direction : [np.arange(num_particles)] for _direction in directions}
        _logger.debug(f"\tsMC_particle_ancestries: {sMC_particle_ancestries}")

        sMC_cumulative_works = {_direction : [np.zeros(num_particles)] for _direction in directions}
        _logger.debug(f"\tsMC_cumulative_works: {sMC_cumulative_works}")

        sMC_observables = {_direction : [1.] for _direction in directions}
        _logger.debug(f"\tsMC_observables: {sMC_observables}")

        omit_local_incremental_append = {_direction: False for _direction in directions}
        last_increment = {_direction: False for _direction in directions}
        worker_retrieval = {}


        #now we can launch annealing jobs and manage them on-the-fly
        current_lambdas = starting_lines
        iteration_number = 0
        #_logger.info(f"current protocols : {self.protocols}")

        if (not _trailblaze) and (not _resample):
            _AIS = True
        else:
            _AIS = False

        while current_lambdas != finish_lines: # respect the while loop; it is a dangerous creature
            _logger.info(f"entering iteration {iteration_number}; current lambdas are: {current_lambdas}")
            start_timer = time.time()
            if _AIS:
                local_incremental_work_collector = {_direction : np.zeros((num_particles, self.protocols[_direction].shape[0])) for _direction in directions}
            else:
                local_incremental_work_collector = {_direction : np.zeros(num_particles) for _direction in directions}

            start_timer = time.time()
            #if trailblaze is true, we have to choose the next lambda from the previous sampler states and weights
            if _trailblaze:
                for _direction in directions:
                    if current_lambdas[_direction] == finish_lines[_direction]: #if this direction is done...
                        _logger.info(f"\tdirection {_direction} is complete.  omitting trailblazing.")
                        continue
                    else: #we have to choose the next lambda value
                        _logger.info(f"\ttrailblazing {_direction}...")
                        #gather sampler states and cumulative works in a concurrent manner (i.e. flatten them)
                        sampler_states = sMC_sampler_states[_direction]
                        cumulative_works = sMC_cumulative_works[_direction][-1]
                        if iteration_number == 0:
                            initial_guess = None
                        else:
                            initial_guess = min([2 * self.protocols[_direction][-1] - self.protocols[_direction][-2], 1.0]) if _direction == 'forward' else max([2 * self.protocols[_direction][-1] - self.protocols[_direction][-2], 0.0])

                        _new_lambda, normalized_observable = self.binary_search(sampler_states = sampler_states,
                                                                                cumulative_works = cumulative_works,
                                                                                start_val = current_lambdas[_direction],
                                                                                end_val = finish_lines[_direction],
                                                                                observable = self.supported_observables[trailblaze['criterion']],
                                                                                observable_threshold = trailblaze['threshold'] * sMC_observables[_direction][-1],
                                                                                initial_guess = initial_guess)
                        _logger.info(f"\tlambda increments: {current_lambdas[_direction]} to {_new_lambda}.")
                        _logger.info(f"\tnormalized observable: {normalized_observable}.  Observable threshold is {trailblaze['threshold'] * sMC_observables[_direction][-1]}")
                        self.protocols[_direction].append(_new_lambda)
                        if not _resample:
                            sMC_observables[_direction].append(normalized_observable)

            for _direction in directions:
                if current_lambdas[_direction] == finish_lines[_direction]:
                    _logger.info(f"\tdirection {_direction} is complete.  omitting annealing")
                    omit_local_incremental_append[_direction] == True
                    continue
                worker_retrieval[_direction] = time.time()
                _logger.info(f"\tentering {_direction} direction to launch annealing jobs.")

                if _AIS:
                    #then we are just doing vanilla AIS, in which case, it is not necessary to perform a single incremental lambda perturbation
                    #instead, we can run the entire defined protocol
                    _lambdas = self.protocols[_direction]
                else:
                    _lambdas = np.array([self.protocols[_direction][iteration_number], self.protocols[_direction][iteration_number + 1]])

                _logger.info(f"\t\tthe current lambdas for annealing are {_lambdas}")

                if self.protocols[_direction][iteration_number + 1] == finish_lines[_direction]:
                    last_increment[_direction] == True

                #make iterable lists for anneal deployment
                iterables = []
                iterables.append([remote_worker] * num_processes) #remote_worker
                iterables.append(sMC_sampler_states[_direction]) #sampler_state
                iterables.append([_lambdas] * num_particles) #lambdas
                iterables.append([None]*num_particles) #noneq_trajectory_filename
                iterables.append([num_integration_steps] * num_particles) #num_integration_steps
                iterables.append([return_timer] * num_particles) #return timer
                iterables.append([True] * num_particles) #return_sampler_state
                iterables.append([rethermalize] * num_particles) #rethermalize
                if _lambdas[0] == starting_lines[_direction]: #initial_propagation
                    iterables.append([True] * num_particles) # propagate the starting lambdas
                else:
                    iterables.append([False] * num_particles) # do not propagate

                for job in range(num_particles):
                    if self.ncmc_save_interval is not None: #check if we should make 'trajectory_filename' not None
                        iterables[2][job] = self.neq_traj_filename[_direction] + f".iteration_{job:04}.h5")


                scattered_futures = [self.parallelism.scatter(iterable) for iterable in iterables]
                sMC_futures[_direction] = self.parallelism.deploy(func = call_anneal_method,
                                                                  arguments = tuple(scattered_futures),
                                                                  workers = workers)

            #collect futures into one list and see progress
            all_futures = [item for sublist in list(sMC_futures.values()) for item in sublist]
            self.parallelism.progress(futures = all_futures)

            #now we collect the finished futures
            for _direction in directions:
                if current_lambdas[_direction] == finish_lines[_direction]:
                    _logger.info(f"\tdirection {_direction} is complete.  omitting job collection")
                    continue
                _futures = self.parallelism.gather_results(futures = sMC_futures[_direction])

                #collect tuple results
                _incremental_works = [iter[0] for iter in _futures]
                _sampler_states = [iter[1] for iter in _futures]
                _timers = [iter[2] for iter in _futures]

                #append the incremental works
                if not _AIS:
                    local_incremental_work_collector[_direction] += np.array(_incremental_works).flatten()
                else:
                    [local_incremental_work_collector[_direction][index, 1:] = _incremental_works[index] for index in range(len(_incremental_works))

                #append the sampler_states
                sMC_sampler_states[_direction] = np.array(_sampler_states)

                #append the _timers
                sMC_timers[_direction].append(_timers)

                print(f"\t{_direction} retrieval time: {time.time() - worker_retrieval[_direction]}")

            #report the updated logger dicts
            for _direction in directions:
                current_lambdas[_direction] = self.protocols[_direction][-1]

            #resample if necessary
            if _resample:
                assert not _AIS, f"attempting to resample, but only AIS is being conducted (sequential importance sampling)"
                for _direction in directions:
                    if current_lambdas[_direction] == finish_lines[_direction] and not last_increment[_direction]:
                        continue

                    if last_increment[_direction] == True:
                        last_increment[_direction] == False

                    normalized_observable_value, resampled_works, resampled_indices = self._resample(incremental_works = local_incremental_work_collector[_direction],
                                                                                                     cumulative_works = sMC_cumulative_works[_direction][-1],
                                                                                                     observable = resample['criterion'],
                                                                                                     resampling_method = resample['method'],
                                                                                                     resample_observable_threshold = resample['threshold'])
                    sMC_observables[_direction].append(normalized_observable_value)

                    sMC_cumulative_works[_direction].append(resampled_works.reshape(num_actors_per_direction, num_particles_per_actor))

                    flattened_sampler_states = sMC_sampler_states[_direction].flatten()
                    new_sampler_states = np.array([flattened_sampler_states[i] for i in resampled_indices]).reshape(num_actors_per_direction, num_particles_per_actor)
                    sMC_sampler_states[_direction] = new_sampler_states

                    flattened_particle_ancestries = sMC_particle_ancestries[_direction][-1].flatten()
                    new_particle_ancestries = np.array([flattened_particle_ancestries[i] for i in resampled_indices]).reshape(num_actors_per_direction, num_particles_per_actor)
                    sMC_particle_ancestries[_direction].append(new_particle_ancestries)
            else:
                if (not _trailblaze) and (not _resample): #we have to make an exception to bookkeeping if we are doing vanilla AIS
                    sMC_cumulative_works = {}
                    cumulative_works = {}
                    for _direction in directions:
                        cumulative_works[_direction] = local_incremental_work_collector[_direction]
                        for i in range(cumulative_works[_direction].shape[0]):
                            for j in range(cumulative_works[_direction].shape[1]):
                                cumulative_works[_direction][i,j,:] = np.cumsum(local_incremental_work_collector[_direction][i,j,:])
                        sMC_cumulative_works[_direction] = [cumulative_works[_direction][:,:,i] for i in range(cumulative_works[_direction].shape[2])]
                else:
                    for _direction in directions:
                        if not  omit_local_incremental_append[_direction]:
                            sMC_cumulative_works[_direction].append(np.add(sMC_cumulative_works[_direction][-1], local_incremental_work_collector[_direction]))

            end_timer = time.time() - start_timer
            iteration_number += 1
            _logger.info(f"iteration took {end_timer} seconds.")

        self.compute_sMC_free_energy(sMC_cumulative_works)
        self.sMC_observables = sMC_observables
        if _resample:
            self.survival_rate = compute_survival_rate(sMC_particle_ancestries)
            self.particle_ancestries = {_direction : np.array([q.flatten() for q in sMC_particle_ancestries[_direction]]) for _direction in sMC_particle_ancestries.keys()}

    def compute_sMC_free_energy(self, cumulative_work_dict):
        _logger.debug(f"computing free energies...")
        self.cumulative_work = {_direction: None for _direction in cumulative_work_dict.keys()}
        self.dg_EXP = {}
        for _direction, _lst in cumulative_work_dict.items():
            flat_cumulative_work = np.array([arr.flatten() for arr in _lst])
            self.cumulative_work[_direction] = flat_cumulative_work
            self.dg_EXP[_direction] = np.array([pymbar.EXP(flat_cumulative_work[i, :]) for i in range(flat_cumulative_work.shape[0])])
        #_logger.info(f"cumulative_work: {self.cumulative_work}")
        if (len(list(self.cumulative_work.keys())) == 2):
            self.dg_BAR = pymbar.BAR(self.cumulative_work['forward'][-1, :], self.cumulative_work['reverse'][-1, :])

    def minimize_sampler_states(self):
        # initialize by minimizing
        for state in self.lambda_endstates['forward']: # 0.0, 1.0
            self.thermodynamic_state.set_alchemical_parameters(state, LambdaProtocol(functions = self.lambda_protocol))
            minimize(self.thermodynamic_state, self.sampler_states[int(state)])

    def pull_trajectory_snapshot(self, endstate):
        """
        Draw randomly a single snapshot from self._eq_files_dict

        Parameters
        ----------
        endstate: int
            lambda endstate from which to extract an equilibrated snapshot, either 0 or 1
        Returns
        -------
        sampler_state: openmmtools.SamplerState
            sampler state with positions and box vectors if applicable
        """
        #pull a random index
        index = random.choice(self._eq_dict[f"{endstate}_decorrelated"])
        files = [key for key in self._eq_files_dict[endstate].keys() if index in self._eq_files_dict[endstate][key]]
        assert len(files) == 1, f"files: {files} doesn't have one entry; index: {index}, eq_files_dict: {self._eq_files_dict[endstate]}"
        file = files[0]
        file_index = self._eq_files_dict[endstate][file].index(index)

        #now we load file as a traj and create a sampler state with it
        traj = md.load_frame(file, file_index)
        positions = traj.openmm_positions(0)
        box_vectors = traj.openmm_boxes(0)
        sampler_state = SamplerState(positions, box_vectors = box_vectors)

        return sampler_state

    def equilibrate(self,
                    n_equilibration_iterations = 1,
                    n_steps_per_equilibration = 5000,
                    endstates = [0,1],
                    max_size = 1024*1e3,
                    decorrelate=False,
                    timer = False,
                    minimize = False):
        """
        Run the equilibrium simulations a specified number of times at the lambda 0, 1 states. This can be used to equilibrate
        the simulation before beginning the free energy calculation.

        Parameters
        ----------
        n_equilibration_iterations : int; default 1
            number of equilibrium simulations to run, each for lambda = 0, 1.
        n_steps_per_equilibration : int, default 5000
            number of integration steps to take in an equilibration iteration
        endstates : list, default [0,1]
            at which endstate(s) to conduct n_equilibration_iterations (either [0] ,[1], or [0,1])
        max_size : float, default 1.024e6 (bytes)
            number of bytes allotted to the current writing-to file before it is finished and a new equilibrium file is initiated.
        decorrelate : bool, default False
            whether to parse all written files serially and remove correlated snapshots; this returns an ensemble of iid samples in theory.
        timer : bool, default False
            whether to trigger the timing in the equilibration; this adds an item to the EquilibriumResult, which is a list of times for various
            processes in the feptask equilibration scheme.
        minimize : bool, default False
            Whether to minimize the sampler state before conducting equilibration. This is passed directly to feptasks.run_equilibration

        Returns
        -------
        equilibrium_result : perses.dispersed.feptasks.EquilibriumResult
            equilibrium result namedtuple
        """

        _logger.debug(f"conducting equilibration")
        for endstate in endstates:
            assert endstate in [0, 1], f"the endstates contains {endstate}, which is not in [0, 1]"

        # run a round of equilibrium
        _logger.debug(f"iterating through endstates to submit equilibrium jobs")
        EquilibriumFEPTask_list = []
        for state in endstates: #iterate through the specified endstates (0 or 1) to create appropriate EquilibriumFEPTask inputs
            _logger.debug(f"\tcreating lambda state {state} EquilibriumFEPTask")
            self.thermodynamic_state.set_alchemical_parameters(float(state), lambda_protocol = LambdaProtocol(functions = self.lambda_protocol))
            input_dict = {'thermodynamic_state': copy.deepcopy(self.thermodynamic_state),
                          'nsteps_equil': n_steps_per_equilibration,
                          'topology': self.factory.hybrid_topology,
                          'n_iterations': n_equilibration_iterations,
                          'splitting': self.eq_splitting_string,
                          'atom_indices_to_save': None,
                          'trajectory_filename': None,
                          'max_size': max_size,
                          'timer': timer,
                          '_minimize': minimize,
                          'file_iterator': 0,
                          'timestep': self.timestep}


            if self.write_traj:
                _logger.debug(f"\twriting traj to {self.eq_trajectory_filename[state]}")
                equilibrium_trajectory_filename = self.eq_trajectory_filename[state]
                input_dict['trajectory_filename'] = equilibrium_trajectory_filename
            else:
                _logger.debug(f"\tnot writing traj")

            if self._eq_dict[state] == []:
                _logger.debug(f"\tself._eq_dict[{state}] is empty; initializing file_iterator at 0 ")
            else:
                last_file_num = int(self._eq_dict[state][-1][0][-7:-3])
                _logger.debug(f"\tlast file number: {last_file_num}; initiating file iterator as {last_file_num + 1}")
                file_iterator = last_file_num + 1
                input_dict['file_iterator'] = file_iterator
            task = EquilibriumFEPTask(sampler_state = self.sampler_states[state], inputs = input_dict, outputs = None)
            EquilibriumFEPTask_list.append(task)

        _logger.debug(f"scattering and mapping run_equilibrium task")
        #we need not concern ourselves with _adaptive here since we are only running vanilla MD on 1 or 2 endstates


        if self.external_parallelism:
            #the client is already active
            #we only run a max of 2 parallel runs at once, so we can pull 2 workers
            num_available_workers = min(len(endstates), len(self.parallelism_parameters['available_workers']))
            workers = np.random.choice(self.parallelism_parameters['available_workers'], size = num_available_workers, replace = False)
            scatter_futures = self.parallelism.scatter(EquilibriumFEPTask_list, workers = workers)
            futures = self.parallelism.deploy(run_equilibrium, (scatter_futures,), workers = workers)
        elif self.internal_parallelism:
            #we have to activate the client
            self.parallelism.activate_client(library = self.parallelism_parameters['library'], num_processes = min(len(endstates), len(self.parallelism_parameters['num_processes'])))
            scatter_futures = elf.parallelism.scatter(EquilibriumFEPTask_list)
            futures = self.parallelism.deploy(run_equilibrium, (scatter_futures,))
        else:
            raise Exception(f"either internal or external parallelism must be True.")

        self.parallelism.progress(futures)
        eq_results = self.parallelism.gather_results(futures)

        if self.internal_parallelism:
            #deactivte the client
            self.parallelism.deactivate_client()
        else:
            #we do not deactivate the external parallelism because the current class has no authority over it.  It simply borrows the allotted workers for a short time
            pass

        #the rest of the function is independent of the dask workers...


        for state, eq_result in zip(endstates, eq_results):
            _logger.debug(f"\tcomputing equilibrium task future for state = {state}")
            self._eq_dict[state].extend(eq_result.outputs['files'])
            self._eq_dict[f"{state}_reduced_potentials"].extend(eq_result.outputs['reduced_potentials'])
            self.sampler_states.update({state: eq_result.sampler_state})
            self._eq_timers[state].append(eq_result.outputs['timers'])

        _logger.debug(f"collections complete.")
        if decorrelate: # if we want to decorrelate all sample
            _logger.debug(f"decorrelating data")
            for state in endstates:
                _logger.debug(f"\tdecorrelating lambda = {state} data.")
                traj_filename = self.eq_trajectory_filename[state]
                if os.path.exists(traj_filename[:-2] + f'0000' + '.h5'):
                    _logger.debug(f"\tfound traj filename: {traj_filename[:-2] + f'0000' + '.h5'}; proceeding...")
                    [t0, g, Neff_max, A_t, uncorrelated_indices] = compute_timeseries(np.array(self._eq_dict[f"{state}_reduced_potentials"]))
                    _logger.debug(f"\tt0: {t0}; Neff_max: {Neff_max}; uncorrelated_indices: {uncorrelated_indices}")
                    self._eq_dict[f"{state}_decorrelated"] = uncorrelated_indices

                    #now we just have to turn the file tuples into an array
                    _logger.debug(f"\treorganizing decorrelated data; files w/ num_snapshots are: {self._eq_dict[state]}")
                    iterator, corrected_dict = 0, {}
                    for tupl in self._eq_dict[state]:
                        new_list = [i + iterator for i in range(tupl[1])]
                        iterator += len(new_list)
                        decorrelated_list = [i for i in new_list if i in uncorrelated_indices]
                        corrected_dict[tupl[0]] = decorrelated_list
                    self._eq_files_dict[state] = corrected_dict
                    _logger.debug(f"\t corrected_dict for state {state}: {corrected_dict}")

    def _resample(self,
                  incremental_works,
                  cumulative_works,
                  observable = 'ESS',
                  resampling_method = 'multinomial',
                  resample_observable_threshold = 0.5):
        """
        Attempt to resample particles given an observable diagnostic and a resampling method.

        Arguments
        ----------
        incremental_works : np.array() of floats
            the incremental work accumulated from importance sampling at time t
        cumulative_works : np.array() of floats
            the cumulative work accumulated from importance sampling from time t = 1 : t-1
        observable : str, default 'ESS'
            the observable used as a resampling diagnostic; this calls a key in supported_observables
        resampling_method: str, default 'multinomial'
            method used to resample, this calls a key in supported_resampling_methods
        resample_observable_threshold : float, default 0.5
            the threshold to diagnose a resampling event.
            If None, will automatically return without observables

        Returns
        -------
        normalized_observable_value: float
            the value of the observable
        resampled_works : np.array() floats
            the resampled total works at iteration t
        resampled_indices : np.array() int
            resampled particle indices


        """
        _logger.debug(f"\tAttempting to resample...")
        num_particles = incremental_works.shape[0]

        normalized_observable_value = self.supported_observables[observable](cumulative_works, incremental_works) / num_particles
        total_works = np.add(cumulative_works, incremental_works)

        #decide whether to resample
        _logger.debug(f"\tnormalized observable value: {normalized_observable_value}")
        if normalized_observable_value <= resample_observable_threshold: #then we resample
            _logger.debug(f"\tnormalized observable value ({normalized_observable_value}) <= {resample_observable_threshold}.  Resampling")

            #resample
            resampled_works, resampled_indices = self.supported_resampling_methods[resampling_method](total_works = total_works.flatten(),
                                                                                                      num_resamples = num_particles)

            normalized_observable_value = 1.0
        else:
            _logger.debug(f"\tnormalized observable value ({normalized_observable_value}) > {resample_observable_threshold}.  Skipping resampling.")
            resampled_works = total_works
            resampled_indices = np.arange(num_particles)
            normalized_observable_value = normalized_observable_value


        return normalized_observable_value, resampled_works, resampled_indices


    def binary_search(self,
                  sampler_states,
                  cumulative_works,
                  start_val,
                  end_val,
                  observable,
                  observable_threshold,
                  max_iterations=20,
                  initial_guess = None,
                  precision_threshold = 1e-6):
        """
        Given corresponding start_val and end_val of observables, conduct a binary search to find min value for which the observable threshold
        is exceeded.
        Arguments
        ----------
        sampler_states : np.array(openmmtools.states.SamplerState)
            numpy array of sampler states
        cumulative_works : np.array(float)
            cumulative works of corresponding sampler states
        start_val: float
            start value of binary search
        end_val: float
            end value of binary search
        observable : function
            function to compute an observable
        observable_threshold : float
            the threshold of the observable used to satisfy the binary search criterion
        max_iterations: int, default 20
            maximum number of interations to conduct
        initial_guess: float, default None
            guess where the threshold is achieved
        precision_threshold: float, default 1e-6
            precision threshold below which, the max iteration will break

        Returns
        -------
        midpoint: float
            maximum value that doesn't exceed threshold
        _observable : float
            observed value of observable
        """
        _base_end_val = end_val
        _logger.debug(f"\t\t\tmin, max values: {start_val}, {end_val}. ")
        self.thermodynamic_state.set_alchemical_parameters(start_val, LambdaProtocol(functions = self.lambda_protocol))
        current_rps = np.array([compute_reduced_potential(self.thermodynamic_state, sampler_state) for sampler_state in sampler_states])

        if initial_guess is not None:
            midpoint = initial_guess
        else:
            midpoint = (start_val + end_val) * 0.5

        for _ in range(max_iterations):
            self.thermodynamic_state.set_alchemical_parameters(midpoint, LambdaProtocol(functions = self.lambda_protocol))
            new_rps = np.array([compute_reduced_potential(self.thermodynamic_state, sampler_state) for sampler_state in sampler_states])
            _observable = observable(cumulative_works, new_rps - current_rps) / len(current_rps)
            if _observable <= observable_threshold:
                end_val = midpoint
            else:
                start_val = midpoint
            midpoint = (start_val + end_val) * 0.5
            if precision_threshold is not None:
                if abs(_base_end_val - midpoint) <= precision_threshold:
                    midpoint = _base_end_val
                    self.thermodynamic_state.set_alchemical_parameters(midpoint, LambdaProtocol(functions = self.lambda_protocol))
                    new_rps = np.array([compute_reduced_potential(self.thermodynamic_state, sampler_state) for sampler_state in sampler_states])
                    _observable = observable(cumulative_works, new_rps - current_rps) / len(current_rps)
                    break
                elif abs(end_val - start_val) <= precision_threshold:
                    midpoint = end_val
                    self.thermodynamic_state.set_alchemical_parameters(midpoint, LambdaProtocol(functions = self.lambda_protocol))
                    new_rps = np.array([compute_reduced_potential(self.thermodynamic_state, sampler_state) for sampler_state in sampler_states])
                    _observable = observable(cumulative_works, new_rps - current_rps) / len(current_rps)
                    break

        return midpoint, _observable
