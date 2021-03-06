"""
Module for skill models

@author Siddharth Reddy <sgr45@cornell.edu>
"""



from abc import abstractmethod
import math
import logging

import numpy as np
from scipy import sparse
from sklearn import cross_validation
from sklearn.linear_model import LogisticRegression

from . import datatools
from . import forget
from . import cgraph


_logger = logging.getLogger(__name__)

# names of parameters for EmbeddingModel
STUDENT_EMBEDDINGS = 'student_embeddings'
ASSESSMENT_EMBEDDINGS = 'assessment_embeddings'
LESSON_EMBEDDINGS = 'lesson_embeddings'
PREREQ_EMBEDDINGS = 'prereq_embeddings'
CONCEPT_EMBEDDINGS = 'concept_embeddings'
STUDENT_BIASES = 'student_biases'
ASSESSMENT_BIASES = 'assessment_biases'

# see docstring for EmbeddingModel.__init__ 
ANTI_SINGULARITY_LOWER_BOUND = 0.001

# names of parameters for MIRTModel
STUDENT_FACTORS = 'student_factors'
ASSESSMENT_FACTORS = 'assessment_factors'
ASSESSMENT_OFFSETS = 'assessment_offsets'


class SkillModel(object):
    """
    Superclass for skill models. A skill model is an object that
    ingests an interaction history, then learns a representation of students
    and content that can be used to predict unobserved assessment outcomes.

    The abstract method assessment_outcome_log_likelihood should output
    the log-likelihood of an assessment outcome given the skill model's
    parameterizations of the student and assessment.
    """

    @abstractmethod
    def assessment_outcome_log_likelihood(
        self,
        interaction,
        outcome=None):
        """
        Compute log-likelihood of assessment outcome

        :param dict[str,object] interaction: An interaction
        :param bool|None outcome: An assessment result
        :rtype: float
        :return: Log-likelihood of outcome, given student and content parameters
        """
        pass

    def assessment_pass_likelihood(
        self,
        interaction):
        """
        Compute the likelihood of passing an assessment interaction

        :param dict[str,object] interaction: An interaction
        :rtype: float
        :return: Likelihood of a passing outcome, given student and content parameters
        """

        return math.exp(self.assessment_outcome_log_likelihood(interaction, outcome=True))

    def assessment_pass_likelihoods(
        self,
        interactions):
        """
        Computes the likelihoods of passing a list of assessment interactions

        :param pd.DataFrame interactions: A dataframe containing rows of interactions
        :rtype: np.array
        :return: Likelihoods of passing outcomes, given student and content parameters
        """
        return np.array(interactions.apply(self.assessment_pass_likelihood, axis=1))


class EmbeddingModel(SkillModel):
    """
    Class for a Latent Skill Embedding model that embeds students,
    assessments, and lessons in a joint semantic space that
    allows us to reason about relationships between students
    and content (i.e., assessment result likelihoods and knowledge gains from lessons)
    """

    def __init__(self,
        history,
        embedding_dimension=2,
        graph=None,
        using_lessons=True,
        using_prereqs=True,
        using_bias=True,
        using_graph_prior=False,
        graph_regularization_constant=0.1,
        using_l1_regularizer=False,
        tv_luv_model=None,
        forgetting_model=None,
        learning_update_variance_constant=0.5,
        forgetting_penalty_term_constant=0.,
        anti_singularity_lower_bound=ANTI_SINGULARITY_LOWER_BOUND):
        """
        Initialize skill model object

        :param datatools.InteractionHistory|None history: An interaction history
        :param int embedding_dimension: The number of dimensions in the latent skill space
        :param cgraph.ConceptGraph|None graph: 
            A content-to-concept map and dependency graph for concepts

        :param bool using_lessons: Include lessons in the embedding
        :param bool using_prereqs: Include lesson prerequisites in the embedding
            This should only be True if using_lessons is True

        :param bool using_bias: Include bias terms in the assessment result likelihood
        :param bool using_graph_prior: Use concept graph to regularize module embeddings
        :param float graph_regularization_constant: Coefficient of graph regularization term
        :param bool using_l1_regularizer:
            True => use L1 regularization on lesson and assessment embeddings
            False => use L2 regularization on lesson and assessment embeddings

        :param forget.ForgettingModel|None forgetting_model: A model of the forgetting effect
            None => use forgetting_penalty_term_constant
        
        :param forget.TimeVaryingLUVModel|None tv_luv_model:
            A model of time-varying learning update variance
            None => use learning_update_variance_constant
        
        :param float learning_update_variance_constant: Variance of the Gaussian learning update
        :param float forgetting_penalty_term_constant:
            Penalty term in mean of Gaussian learning update
        
        :param float anti_singularity_lower_bound:
            Embedding parameters live in \mathbb{R}^d_+, but allowing assessments and lesson 
            prerequisites to get close to zero can lead to a singularity in the embedding distance
            (the norms of the assessment and prereq embeddings are in denominators). To avoid this,
            we constrain assessments and prereqs to be > than a strictly positive lower bound
            (while other embedding parameters are constrained to be non-negative)
        """
        if embedding_dimension<=0:
            raise ValueError('Embedding dimension is strictly positive!')
        if learning_update_variance_constant<=0:
            raise ValueError('Learning update variance is strictly positive!')
        if anti_singularity_lower_bound<=0:
            raise ValueError('Lower bound on assessment/prereq embeddings is strictly positive!')
        if using_graph_prior and graph is None:
            raise ValueError('Must supply graph if using_graph_prior=True!')
        if using_prereqs and not using_lessons:
            raise ValueError('Cannot model lesson prerequisites without lesson embeddings!')

        if using_graph_prior:
            _logger.warning('Correctness of gradients for the graph prior not verified!')

        self.history = history
        self.embedding_dimension = embedding_dimension
        self.graph = graph
        self.graph_regularization_constant = graph_regularization_constant
        self.anti_singularity_lower_bounds = {
            STUDENT_EMBEDDINGS : 0,
            ASSESSMENT_EMBEDDINGS : anti_singularity_lower_bound,
            LESSON_EMBEDDINGS : 0,
            PREREQ_EMBEDDINGS : anti_singularity_lower_bound,
            CONCEPT_EMBEDDINGS : anti_singularity_lower_bound
        }
        self.using_prereqs = using_prereqs
        self.using_lessons = using_lessons
        self.using_bias = using_bias
        self.using_graph_prior = using_graph_prior
        self.using_l1_regularizer = using_l1_regularizer

        self.forgetting_model = forgetting_model
        self.forgetting_penalty_term_constant = forgetting_penalty_term_constant

        self.tv_luv_model = tv_luv_model
        self.learning_update_variance_constant = learning_update_variance_constant

        if self.using_graph_prior:
            self.concept_embeddings = None

        # student tensor
        # student_idx, skillidx, timestep -> skill level
        self.student_embeddings = None

        # assessment matrix
        # assessment_idx, skillidx -> skill requirement
        self.assessment_embeddings = None

        # student bias terms
        # student_idx -> bias
        self.student_biases = None

        # assessment bias terms
        # assessment_idx -> bias
        self.assessment_biases = None

        # lesson matrix
        # lesson_idx, skillidx -> skill gain
        if self.using_lessons:
            self.lesson_embeddings = None

        # lesson prereq matrix
        # lesson_idx, skillidx -> skill requirement
        if self.using_prereqs:
            self.prereq_embeddings = None

        if self.using_graph_prior:
            self.concept_embeddings = None

        if self.history is not None:
            num_students = self.history.num_students()
            self.student_embeddings = np.zeros(
                    (num_students, self.embedding_dimension, self.history.duration()))

            num_assessments = self.history.num_assessments()
            self.assessment_embeddings = np.zeros((num_assessments, self.embedding_dimension))

            self.student_biases = np.zeros(num_students)
            self.assessment_biases = np.zeros(num_assessments)

            num_lessons = self.history.num_lessons()
            if self.using_lessons:
                self.lesson_embeddings = np.zeros((num_lessons, self.embedding_dimension))

            if self.using_prereqs:
                self.prereq_embeddings = np.zeros((num_lessons, self.embedding_dimension))

            if self.using_graph_prior:
                num_concepts = len(self.graph.idx_of_concept_id)
                self.concept_embeddings = np.zeros((num_concepts, self.embedding_dimension))

    def learning_update_variance(self, times_since_prev_ixn_for_lesson_ixns):
        """
        Compute variances of Gaussian learning updates

        :param np.array times_since_prev_ixn_for_lesson_ixns:
            Time since previous interaction, for each lesson interaction
        
        :rtype: np.ndarray
        :return: A column vector of variances, one for each lesson interaction
        """
        if self.tv_luv_model is None:
            return self.learning_update_variance_constant

        return self.tv_luv_model.learning_update_variances(
            times_since_prev_ixn_for_lesson_ixns)[:, None]

    def forgetting_penalty_terms(self, times_since_prev_ixn_for_lesson_ixns):
        """
        Compute forgetting penalties of Gaussian learning updates

        :param np.array times_since_prev_ixn_for_lesson_ixns:
            Time since previous interaction (for each lesson interaction)
        
        :rtype: np.ndarray
        :return:
            A two-dimensional array with shape
            (num_lesson_interactions, embedding_dimension)
            containing the forgetting penalty term for each lesson interaction
        """
        if self.forgetting_model is None:
            return self.forgetting_penalty_term_constant

        return self.forgetting_model.penalty_terms(times_since_prev_ixn_for_lesson_ixns)[:, None]

    def concept_assessment_edges_in_graph(self):
        """
        Get a list of concept-assessment edges in the graph

        :rtype: (np.array,np.array,int,int,np.array)
        :return: A tuple of (assessment indexes, concept indexes,
            number of unique assessments, number of unique concepts,
            number of concepts for each assessment in the first array of this tuple)
        """

        return self.graph.concept_module_edges(
            self.history.iter_assessments,
            self.history.idx_of_assessment_id)

    def concept_lesson_edges_in_graph(self):
        """
        Get a list of concept-lesson edges in the graph

        :rtype: (np.array,np.array,int,int,np.array)
        :return: A tuple of (lesson indexes, concept indexes,
            number of unique lessons, number of unique concepts,
            number of concepts for each lesson in the first array of this tuple)
        """

        return self.graph.concept_module_edges(
            self.history.iter_lessons,
            self.history.idx_of_lesson_id)

    def fit(self, estimator):
        """
        Fit skill embedding model to its interaction history

        :param est.EmbeddingModelEstimator estimator:
            A skill embedding model estimator
        """

        estimator.fit_model(self)

    def embedding_distance(
        self,
        student_embedding,
        module_embedding):
        """
        Compute the distance between a student
        and an assessment (or prereq) in the latent skill space

        dist(s, a) = (s dot a) / ||a|| - ||a||

        :param np.ndarray student_embedding: A student embedding
        :param np.ndarray module_embedding: An assessment (or prereq) embedding
        :rtype: float
        :return: Distance in the latent skill space
        """

        module_embedding_norm = np.linalg.norm(module_embedding)

        return np.dot(
            student_embedding,
            module_embedding) / module_embedding_norm - module_embedding_norm

    def assessment_outcome_log_likelihood(
        self,
        interaction,
        outcome=None):
        """
        Compute log-likelihood of assessment interaction, given the embedding

        :param dict[str,object] interaction: An interaction
        :param bool|None outcome:
            If outcome is a bool, it overrides interaction['outcome'].
            This is useful for :py:func:`models.SkillModel.assessment_pass_likelihood`
        
        :rtype: float|np.nan
        :return: Log-likelihood of outcome, given the embedding
            If computing the log-likelihood results in a numerical error
            (e.g., overflow or underflow), then np.nan is returned
        """
        try:
            student_id = interaction['student_id']
            timestep = interaction['timestep']
            assessment_id = interaction['module_id']
            outcome = 1 if (interaction['outcome'] if outcome is None else outcome) else -1
        except KeyError:
            raise ValueError('Interaction is missing fields!')

        student_idx = self.history.idx_of_student_id(student_id)
        assessment_idx = self.history.idx_of_assessment_id(assessment_id)

        return self.assessment_outcome_log_likelihood_helper(
            self.student_embeddings[student_idx, :, timestep],
            self.assessment_embeddings[assessment_idx, :],
            self.student_biases[student_idx],
            self.assessment_biases[assessment_idx],
            outcome)

    def assessment_outcome_log_likelihood_helper(
        self,
        student_during,
        requirements_of_assessment,
        student_bias,
        assessment_bias,
        outcome):

        delta = self.embedding_distance(
                student_during, requirements_of_assessment) + student_bias + assessment_bias

        try:
            return -math.log(1 + math.exp(-outcome * delta))
        except: # overflow or underflow
            return np.nan

    def prereq_weight(
        self,
        prev_student_embedding,
        prereq_embedding):

        return 1 / (1 + math.exp(
            -self.embedding_distance(prev_student_embedding, prereq_embedding)))

    def assessment_pass_likelihoods(self, df):
        """
        Compute pass likelihoods of a set of assessments, given trained model parameters

        :param pd.DataFrame df: A set of assessment interactions
        :rtype: np.array
        :return: A list of pass likelihoods
        """

        student_idxes = df['student_id'].apply(self.history.idx_of_student_id)
        assessment_idxes = df['module_id'].apply(self.history.idx_of_assessment_id)

        student_embeddings_of_ixns = self.student_embeddings[student_idxes, :, df['timestep']]
        assessment_embeddings_of_ixns = self.assessment_embeddings[assessment_idxes, :]
        assessment_embedding_norms_of_ixns = np.linalg.norm(assessment_embeddings_of_ixns, axis=1)
        if self.using_bias:
            student_biases_of_ixns = self.student_biases[student_idxes]
            assessment_biases_of_ixns = self.assessment_biases[assessment_idxes]
        else:
            student_biases_of_ixns = assessment_biases_of_ixns = 0

        return 1 / (1 + np.exp(-(
            np.einsum('ij, ij->i', student_embeddings_of_ixns, assessment_embeddings_of_ixns) / \
                    assessment_embedding_norms_of_ixns - assessment_embedding_norms_of_ixns + \
                    student_biases_of_ixns + assessment_biases_of_ixns)))


class StudentBiasedCoinModel(SkillModel):
    """
    Class for simple skill model where students are modeled as biased
    coins that flip to pass/fail assessments
    
    Can be considered a zero-parameter logistic model from Item Response Theory (0PL IRT)
    """

    def __init__(
        self,
        history,
        filtered_history=None,
        name_of_user_id='student_id'):
        """
        Initialize skill model object

        :param datatools.InteractionHistory history: An interaction history
        :param pd.DataFrame|None filtered_history: A filtered interaction history
        """

        self.history = history
        if filtered_history is None:
            _logger.warning(
                'No filtered history available to train biased coin model. Using full history...')
            self.filtered_history = history.data
        else:
            self.filtered_history = filtered_history

        self.name_of_user_id = name_of_user_id

        # student_idx -> probability of the student passing any assessment
        self.idx_of_user_id = {k: i for i, k in enumerate(self.history.data[self.name_of_user_id].unique())}
        self._student_pass_likelihoods = np.zeros(len(self.idx_of_user_id))

    def fit(self):
        """
        Estimate pass likelihood for each student
        """

        df = self.filtered_history[self.filtered_history['module_type'] == \
                datatools.AssessmentInteraction.MODULETYPE]
        df = df.groupby(self.name_of_user_id)

        def student_pass_rate(student_id):
            """
            Get pass rate of student, using Laplace smoothing

            :param str student_id: An id of a student
            :rtype: float
            :return: Smoothed pass rate of student
            """
            try:
                outcomes = df.get_group(student_id)['outcome']
            except KeyError: # student only has lesson interactions (no assessments)
                return 0.5
            try:
                num_passes = outcomes.value_counts()[True]
            except: # student never passed :(
                num_passes = 0
            return (num_passes + 1) / (len(outcomes) + 2)

        for user_id, user_idx in self.idx_of_user_id.items():
            self._student_pass_likelihoods[user_idx] = student_pass_rate(user_id)

    def assessment_outcome_log_likelihood(
        self,
        interaction,
        outcome=None):
        """
        Compute log-likelihood of assessment interaction, given student pass rate

        :param dict interaction: An interaction

        :param bool|None outcome:
            If outcome is a bool, it overrides interaction['outcome'].
            This is useful for :py:func:`models.SkillModel.assessment_pass_likelihood`

        :rtype: float
        :return: Log-likelihood of assessment result, given student pass rate
        """
        try:
            if outcome is None:
                outcome = interaction['outcome']
            user_id = interaction[self.name_of_user_id]
        except KeyError:
            raise ValueError('Interaction is missing fields!')

        student_idx = self.idx_of_user_id[student_id]
        pass_likelihood = self._student_pass_likelihoods[student_idx]
        outcome_likelihood = pass_likelihood if outcome else (1 - pass_likelihood)

        return math.log(outcome_likelihood)

    def assessment_pass_likelihoods(self, df):
        """
        Compute pass likelihoods of a set of assessments, given trained model parameters

        :param pd.DataFrame df: A set of assessment interactions
        :rtype: np.array
        :return: A list of pass likelihoods
        """

        return np.array([self._student_pass_likelihoods[user_idx] for user_idx in \
                df[self.name_of_user_id].map(self.idx_of_user_id)])

class AssessmentBiasedCoinModel(SkillModel):
    """
    Class for simple skill model where assessments are modeled as biased
    coins that flip to pass/fail students

    Can be considered a zero-parameter logistic model from Item Response Theory (0PL IRT)
    """

    def __init__(
        self,
        history,
        filtered_history=None):
        """
        Initialize skill model object

        :param datatools.InteractionHistory history: An interaction history
        :param pd.DataFrame|None filtered_history: A filtered interaction history
        """

        self.history = history
        if filtered_history is None:
            _logger.warning(
                'No filtered history available to train biased coin model. Using full history...')
            self.filtered_history = history.data
        else:
            self.filtered_history = filtered_history

        # assessment_idx -> probability of the assessment being passed by any student
        self._assessment_pass_likelihoods = np.zeros(self.history.num_assessments())

    def fit(self):
        """
        Estimate pass likelihood for each assessment
        """

        df = self.filtered_history[self.filtered_history['module_type'] == \
                datatools.AssessmentInteraction.MODULETYPE]
        df = df.groupby('module_id')

        def assessment_pass_rate(assessment_id):
            """
            Get pass rate of assessment, using Laplace smoothing

            :param str assessment_id: An id of an assessment
            :rtype: float
            :return: Smoothed pass rate of assessment
            """
            try:
                outcomes = df.get_group(assessment_id)['outcome']
            except KeyError:
                return 0.5
            try:
                num_passes = outcomes.value_counts()[True]
            except: # this assessment was never passed
                num_passes = 0
            return (num_passes + 1) / (len(outcomes) + 2)

        for assessment_id in self.history.iter_assessments():
            self._assessment_pass_likelihoods[self.history.idx_of_assessment_id(
                assessment_id)] = assessment_pass_rate(assessment_id)

    def assessment_outcome_log_likelihood(
        self,
        interaction,
        outcome=None):
        """
        Compute log-likelihood of assessment interaction, given assessment pass rate

        :param dict interaction: An interaction

        :param bool|None outcome:
            If outcome is a bool, it overrides interaction['outcome'].
            This is useful for :py:func:`models.SkillModel.assessment_pass_likelihood`

        :rtype: float
        :return: Log-likelihood of assessment result, given assessment pass rate
        """
        try:
            if outcome is None:
                outcome = interaction['outcome']
            assessment_id = interaction['module_id']
        except KeyError:
            raise ValueError('Interaction is missing fields!')

        assessment_idx = self.history.idx_of_assessment_id(assessment_id)
        pass_likelihood = self._assessment_pass_likelihoods[assessment_idx]
        outcome_likelihood = pass_likelihood if outcome else (1 - pass_likelihood)

        return math.log(outcome_likelihood)

    def assessment_pass_likelihoods(self, df):
        """
        Compute pass likelihoods of a set of assessments, given trained model parameters

        :param pd.DataFrame df: A set of assessment interactions
        :rtype: np.array
        :return: A list of pass likelihoods
        """

        return np.array([self._assessment_pass_likelihoods[assessment_idx] for assessment_idx in \
                df['module_id'].apply(self.history.idx_of_assessment_id)])

class IRTModel(SkillModel):
    """
    Superclass for {1, 2}-parameter logistic models of binary response correctness
    in Item Response Theory
    """

    def __init__(
        self,
        history,
        select_regularization_constant=False,
        name_of_user_id='student_id'):
        """
        Initialize IRT model

        :param pd.DataFrame history: A dataframe from an interaction history
        :param bool select_regularization_constant:
            True => select the L2 regularization constant that maximizes average log-likelihood 
            on a validation set
            
            False => use default regularization constant 1.
        """

        self.history = history[history['module_type']==datatools.AssessmentInteraction.MODULETYPE]
        self.history.index = list(range(len(self.history)))

        self.select_regularization_constant = select_regularization_constant
        self.name_of_user_id = name_of_user_id

        self.model = None

        # need to use history['student_id'] since there might be students 
        # with only lesson interactions. Note that we still want to estimate proficiencies for
        # these students, but they will get regularized to zero due to the absence
        # of any assessment interactions.
        self.idx_of_student_id = {k: i for i, k in enumerate(history[self.name_of_user_id].unique())}
        self.num_students = len(self.idx_of_student_id)
        self.idx_of_assessment_id = {k: i for i, k in enumerate(history['module_id'].unique())}
        self.num_assessments = len(self.idx_of_assessment_id)

    @abstractmethod
    def feature_matrix_from_interactions(self, df):
        """
        Construct sparse feature matrix for a set of assessment interactions

        :param pd.DataFrame df: A set of assessment interactions
        
        :rtype: sparse.csr_matrix
        :return: A sparse array of dimensions [n_samples] X [n_features]
        """
        return

    def fit(self):
        """
        Estimate model parameters that fit the interaction history in self.history
        """
        X = self.feature_matrix_from_interactions(self.history)
        Y = np.array(self.history['outcome'].apply(lambda x: 1 if x else 0).values)

        Cs = [0.1, 1., 10.]
        def val_log_likelihood(C):
            """
            Compute average log-likelihood of IRT model with a specific
            regularization constant on a validation set

            :param float C: Coefficient of L2 regularization term
            :rtype: float
            :return: Average log-likelihood on validation set
            """
            train_idxes, val_idxes = cross_validation.train_test_split(
                np.arange(0, len(self.history), 1), train_size=0.7)
            model = LogisticRegression(penalty='l2', C=C)
            X_train = self.feature_matrix_from_interactions(self.history.ix[train_idxes])
            model.fit(X_train, Y[train_idxes])
            X_val = self.feature_matrix_from_interactions(self.history.ix[val_idxes])
            log_probas = model.predict_log_proba(X_val)
            idx_of_zero = 1 if model.classes_[1]==0 else 0
            return np.mean(log_probas[np.arange(0, len(val_idxes), 1), idx_of_zero ^ Y[val_idxes]])

        self.model = LogisticRegression(penalty='l2', C=(
            1. if not self.select_regularization_constant else max(Cs, key=val_log_likelihood)))

        self.model.fit(X, Y)

    def assessment_outcome_log_likelihood(self, interaction, outcome=None):
        """
        Compute the log-likelihood of an assessment outcome, given trained model parameters

        :param dict[str,object] interaction: A single interaction
        :param bool|None outcome: If not None, then overrides interaction['outcome']
        
        :rtype: float
        :return: Log-likelihood of outcome that occurred, under the model
        """

        X = np.zeros(self.num_students+self.num_assessments)
        X[self.idx_of_student_id[interaction[self.name_of_user_id]]] = \
                X[self.idx_of_assessment_id[interaction['module_id']]] = 1

        log_proba = self.model.predict_log_proba(X)

        idx_of_zero = 1 if self.model.classes_[1]==0 else 0

        return log_proba[0, idx_of_zero ^ (1 if (
            interaction['outcome'] if outcome is None else outcome) else 0)]

    def assessment_pass_likelihoods(self, df):
        """
        Compute pass likelihoods of a set of assessments, given trained model parameters

        :param pd.DataFrame df: A set of assessment interactions
        :rtype: list[float]
        :return: A list of pass likelihoods
        """
        X = self.feature_matrix_from_interactions(df)
        probas = self.model.predict_proba(X)
        idx_of_one = 1 if self.model.classes_[1]==1 else 0
        return probas[:, idx_of_one]

class OneParameterLogisticModel(IRTModel):
    """
    Class for one-parameter logistic item response theory (1PL IRT)
    model of binary response correctness
    """

    def feature_matrix_from_interactions(self, df):
        """
        Construct sparse feature matrix for a set of assessment interactions

        The feature vector for an interaction is a binary vector with values for each student 
        (proficiency) and each assessment (difficulty)

        :param pd.DataFrame df: A set of assessment interactions
        :rtype: sparse.csr_matrix
        :return: A sparse array of dimensions [n_samples] X [n_features]
        """

        student_idxes = np.array(df[self.name_of_user_id].map(self.idx_of_student_id).values)
        assessment_idxes = np.array(df['module_id'].map(self.idx_of_assessment_id).values)

        num_ixns = len(df)
        ixn_idxes = np.concatenate((list(range(num_ixns)), list(range(num_ixns))), axis=0)
        studa_idxes = np.concatenate((
                student_idxes, self.num_students + assessment_idxes), axis=0)

        return sparse.coo_matrix(
            (np.ones(2*num_ixns), (ixn_idxes, studa_idxes)),
            shape=(num_ixns, self.num_students + self.num_assessments)).tocsr()

class TwoParameterLogisticModel(IRTModel):
    """
    Class for two-parameter logistic item response theory (1PL IRT)
    model of binary response correctness
    """

    def feature_matrix_from_interactions(self, df):
        """
        Construct sparse feature matrix for a set of assessment interactions

        The feature vector for an interaction is a binary vector with values for each assessment 
        (difficulty) and each student-assessment (proficiency & discriminability) pair

        :param pd.DataFrame df: A set of assessment interactions 
        :rtype: sparse.csr_matrix
        :return: A sparse array of dimensions [n_samples] X [n_features]
        """

        student_idxes = np.array(df[self.name_of_user_id].map(self.idx_of_student_id).values)
        assessment_idxes = np.array(df['module_id'].map(self.idx_of_assessment_id).values)

        num_ixns = len(df)
        ixn_idxes = np.concatenate((list(range(num_ixns)), list(range(num_ixns))), axis=0)
        studa_idxes = np.concatenate((
                student_idxes * self.num_assessments + assessment_idxes,
                self.num_students * self.num_assessments + assessment_idxes), axis=0)

        return sparse.coo_matrix(
            (np.ones(2*num_ixns), (ixn_idxes, studa_idxes)),
            shape=(num_ixns, (self.num_students + 1) * self.num_assessments)).tocsr()


class MIRTModel(object):
    """
    Class for multi-dimensional item response theory (MIRT) model,
    which is a generalization of the uni-dimensional two-parameter logistic model (2PL)

    The item response function becomes Pr(pass) = f((S dot A) + offset)
    where S is the d-dimensional vector for the student, A is the d-dimensional vector 
    for the assessment, the offset is an assessment-specific constant, 
    and f is the logistic function
    """

    def __init__(self, history, dims=2, using_assessment_factors=True):
        """
        Initialize model object

        :param datatools.InteractionHistory history: An interaction history
        :param int dims: Number of dimensions
        :param bool using_assessment_factors: 
            False => set assessment factors to 1, which is helpful
            for simulating 1PL IRT (along with setting dims = 1)
        """

        self.history = history
        self.dims = dims
        self.using_assessment_factors = using_assessment_factors

        self.student_factors = np.zeros((self.history.num_students(), self.dims))
        self.assessment_factors = np.zeros((self.history.num_assessments(), self.dims))
        self.assessment_offsets = np.zeros(self.history.num_assessments())

    def fit(self, estimator):
        """
        Fit model parameters to interaction history

        :param est.MIRTMAPEstimator estimator: An object for parameter estimation
        """

        estimator.fit_model(self)

    def assessment_pass_likelihoods(self, df):
        """
        Compute pass likelihoods of assessment interactions, given trained model parameters

        :param pd.DataFrame df: A set of assessment interactions
        :rtype: np.array
        :return: A list of pass likelihoods
        """

        student_idxes = np.array(df['student_id'].apply(self.history.idx_of_student_id).values)
        assessment_idxes = np.array(df['module_id'].apply(
            self.history.idx_of_assessment_id).values)
        
        student_factors_of_ixns = self.student_factors[student_idxes, :]
        assessment_factors_of_ixns = self.assessment_factors[assessment_idxes, :]
        assessment_offsets_of_ixns = self.assessment_offsets[assessment_idxes]

        return 1 / (1 + np.exp(-(np.einsum(
            'ij, ij->i', 
            student_factors_of_ixns, assessment_factors_of_ixns) + assessment_offsets_of_ixns)))

