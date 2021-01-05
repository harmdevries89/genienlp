from typing import Callable, Iterable, List, Tuple, Union
import xgboost as xgb
import numpy as np
import dill
import pickle
import torch
import itertools
from sklearn.metrics import accuracy_score, confusion_matrix, precision_recall_curve, auc
from sklearn.model_selection import train_test_split
from .util import ConfidenceOutput
import logging

logger = logging.getLogger(__name__)


# Feature function builders
# drop means after applying dropout, nodrop means before

def max_of(f: Callable) -> Callable:
    return lambda x: f(x).max().view(-1)

def min_of(f: Callable) -> Callable:
    return lambda x: f(x).min().view(-1)

def neg_of(f: Callable) -> Callable:
    return lambda x: -f(x)

def cv_drop_logit(i: int) -> Callable:
    def f(x):
        a = torch.sqrt(var_drop_logit(i)(x)) / mean_drop_logit(i)(x)
        a[a!=a] = 0 # set NaNs to zero
        return a
    return f

def var_drop_logit(i: int) -> Callable:
    return lambda x: torch.var(x[i].drop_logits, dim=0).view(-1)

def mean_drop_logit(i: int) -> Callable:
    return lambda x: torch.mean(x[i].drop_logits, dim=0).view(-1)

def nodrop_entropies(i: int) -> Callable:
    return lambda x: x[i].nodrop_entropies

def nodrop_logit(i: int) -> Callable:
    return lambda x: x[i].nodrop_logits

def prediction_length(i: int) -> Callable:
    return lambda x: torch.tensor(len(x[i].nodrop_logits)).view(-1)

def input_length(i: int) -> Callable:
    return lambda x: torch.tensor(len(x[i].context)).view(-1)

def nodrop_avg_logprob(i: int):
    return lambda x: torch.mean(x[i].nodrop_logits).view(-1)

def variance_of_beam_logits(x):
    a = torch.var(torch.tensor([torch.mean(x[i].nodrop_logits).item() for i in range(1, 5)])).view(-1)
    return a

def variance_of_beam_probs(x):
    a = torch.var(torch.tensor([torch.mean(x[i].nodrop_probs).item() for i in range(1, 5)])).view(-1)
    return a

def mean_drop_avg_logprob(i):
    return lambda x: torch.mean(x[i].drop_logits).view(-1)

def var_drop_avg_logprob(i):
    return lambda x: torch.var(torch.mean(x[i].drop_logits, dim=1)).view(-1)

def cv_drop_avg_logprob(i):
    def f(x):
        a = torch.sqrt(var_drop_avg_logprob(i)(x)) / mean_drop_avg_logprob(i)(x)
        a[a!=a] = 0 # set NaNs to zero
        return a
    return f

def nodrop_seq_prob(i):
    return lambda x: torch.prod(x[i].nodrop_probs).view(-1)

def mean_drop_seq_prob(i):
    return lambda x: torch.mean(torch.prod(x[i].drop_probs, dim=1)).view(-1)

def mean_drop_prob(i):
    return lambda x: torch.mean(x[i].drop_probs, dim=0).view(-1)

def var_drop_seq_prob(i):
    return lambda x: torch.var(torch.prod(x[i].drop_probs, dim=1)).view(-1)

def var_drop_prob(i):
    return lambda x: torch.var(x[i].drop_probs, dim=0).view(-1)

def cv_drop_seq_prob(i):
    def f(x):
        a = torch.sqrt(var_drop_seq_prob(i)(x)) / mean_drop_seq_prob(i)(x)
        a[a!=a] = 0 # set NaNs to zero
        return a
    return f

def cev_drop_seq_prob(i):
    """
    Introduced in https://arxiv.org/pdf/1909.00157.pdf
    """
    def f(x):
        a = torch.square(1 - (var_drop_seq_prob(i)(x) / mean_drop_seq_prob(i)(x)))
        a[a!=a] = 0 # set NaNs to zero
        return a
    return f

def cev_drop_prob(i):
    """
    Introduced in https://arxiv.org/pdf/1909.00157.pdf
    """
    def f(x):
        a = torch.square(1 - (var_drop_prob(i)(x) / mean_drop_prob(i)(x)))
        a[a!=a] = 0 # set NaNs to zero
        return a
    return f


def accuracy_at_pass_rate(labels, confidence_scores):
    sorted_confidence_scores, sorted_labels = zip(*sorted(zip(confidence_scores, labels)))
    sorted_labels = np.array(sorted_labels, dtype=np.int)
    # print('sorted_confidence_scores = ', sorted_confidence_scores)
    # print('sorted_labels = ', sorted_labels)
    all_pass_rates = []
    all_accuracies = []
    for i in range(len(sorted_labels)):
        pass_labels = sorted_labels[i:]
        pass_rate = len(pass_labels) / len(sorted_labels)
        all_pass_rates.append(pass_rate)
        accuracy = np.sum(pass_labels) / len(pass_labels)
        all_accuracies.append(accuracy)

    return all_pass_rates, all_accuracies

def oracle_score(confidence: ConfidenceOutput):
    label = ConfidenceEstimator.convert_to_labels([confidence])[0]
    # assign confidence scores randomly in (0, 0.5) for incorrect examples and in (0.5, 1) for correct ones.
    # This way, all correct exampels are ranked above all incorrect examples.
    oracle_confidence = label*(np.random.random()/2+0.5) + (1-label)*(np.random.random()/2)
    return oracle_confidence

def evaluate_raw(dev_confidences: Iterable[ConfidenceOutput], featurizer: Callable):
    """
    Evaluates scores directly, instead of feedeing them into a boosted tree
    """
    dev_labels = ConfidenceEstimator.convert_to_labels(dev_confidences)
    dev_avg_logprobs = [featurizer(c) for c in dev_confidences]
    _max = np.max(dev_avg_logprobs)
    _min = np.min(dev_avg_logprobs)
    dev_avg_logprobs = (dev_avg_logprobs - _min) / (_max - _min)
    precision, recall, thresholds = precision_recall_curve(dev_labels, dev_avg_logprobs)
    pass_rate, accuracies = accuracy_at_pass_rate(dev_labels, dev_avg_logprobs)
    return precision, recall, pass_rate, accuracies, thresholds

def parse_argv(parser):
    parser.add_argument('--confidence_path', type=str, help='The path to the pickle file where the list of ConfidenceOutput objects is saved')
    parser.add_argument('--eval_metric', type=str, default='aucpr', help='An xgboost metric.'
                        'The metric which will be used to select the best model on the validation set.')
    parser.add_argument('--dev_split', type=float, default=0.2, help='The portion of the dataset to use for validation. The rest is used to train.')
    parser.add_argument('--seed', type=int, default=123, help='Random seed to use for reproducibility')
    parser.add_argument('--save', type=str, help='A pickle file to save the calibrator model after training')
    parser.add_argument('--plot', action='store_true', help='If True, will plot metrics and save them. Requires Matplotlib installation.')


class ConfidenceEstimator():
    def __init__(self, name:str, featurizers: List[Union[Callable, Tuple[Callable, Callable]]], eval_metric: str):
        self.name = name
        self.featurizers = featurizers
        self.eval_metric = eval_metric

        self.model = None
        self.score = 0
        self.feature_size = 0
        self.normalizer_sub = 0
        self.normalizer_divide = 1

    @staticmethod
    def _extract_confidence_scores(model, dev_dataset):
        prediction_probs = model.predict(dev_dataset, ntree_limit=model.best_ntree_limit)
        return prediction_probs

    def _pad_and_normalize(self, features: List, normalize='var', train: bool = False):
        if train:
            self.feature_size = max([len(f) for f in features])
            logger.info('feature size of the model is set to %d', self.feature_size)
        
        padded_features = []
        for f in features:
            f = f[:self.feature_size] # truncate
            padded_features.append(np.pad(f, pad_width=(0, self.feature_size-len(f)), constant_values=np.nan, mode='constant'))

        padded_features = np.stack(padded_features)
        if train:
            if normalize == 'var':
                mean = np.nanmean(padded_features, axis=0)
                var = np.nanvar(padded_features, axis=0)
                self.normalizer_sub = mean
                self.normalizer_div = np.sqrt(var)
            elif normalize == 'max':
                _max = np.max(padded_features, axis=0)
                _min = np.min(padded_features, axis=0)
                self.normalizer_sub = _min
                self.normalizer_div = _max - _min
            elif normalize == 'none':
                self.normalizer_sub = 0
                self.normalizer_div = 1
            else:
                raise ValueError('Unexpected value for `normalize`')
        padded_features = (padded_features - self.normalizer_sub) / self.normalizer_div
        padded_features[np.isnan(padded_features)] = 0
        
        return padded_features

    @staticmethod
    def _interleave_features(features_list: List[List]) -> List:
        all_interleaved = []
        for i in range(len(features_list[0])):
            interleaved_length = features_list[0][i].shape[0] * len(features_list)
            interleaved = np.empty((interleaved_length, ), dtype=np.float32)
            for j in range(len(features_list)):
                interleaved[j::len(features_list)] = features_list[j][i]
            all_interleaved.append(interleaved)

        return all_interleaved

    @staticmethod
    def _concatenate(features_list: List[List]) -> List:
        all_concats = []
        for i in range(len(features_list[0])):
            concat = np.concatenate([features_list[j][i] for j in range(len(features_list))])
            all_concats.append(concat)

        return all_concats

    def _convert_to_features(self, confidences: Iterable[ConfidenceOutput], train: bool):
        # TODO check to make sure padding is always on the right hand side, not in the middle of features
        features = []
        for featurizer in self.featurizers:
            if isinstance(featurizer, tuple):
                feature = ConfidenceEstimator._interleave_features([[f(c) for c in confidences] for f in featurizer]) # list of np.arrays
            else:
                feature = [featurizer(c) for c in confidences] # list of np.arrays
            features.append(feature)
        features = ConfidenceEstimator._concatenate(features)
        padded_features = self._pad_and_normalize(features, train=train)
        # print('concatentated features = ', features)
        # print('padded_features = ', padded_features)

        return padded_features

    def _tune_and_train(self, train_dataset, dev_dataset, dev_labels, scale_pos_weight :float):
        # set of all possible hyperparameters
        max_depth = [3, 5, 7, 10, 20, 30, 50] # the maximum depth of each tree
        eta = [0.02, 0.1, 0.5, 0.7] # the training step for each iteration
        num_round = [300]

        best_score = 0
        best_model = None
        best_confusion_matrix = None
        best_params = None
        for m, e, n in itertools.product(max_depth, eta, num_round):
            params = {
                'max_depth': m,  
                'eta': e,  
                'objective': 'binary:logistic',
                'eval_metric': self.eval_metric,
                'scale_pos_weight': scale_pos_weight
                }
            evals_result = {}
            model = xgb.train(params=params,
                            dtrain=train_dataset,
                            evals=[(dev_dataset, 'dev')],
                            num_boost_round=n, 
                            early_stopping_rounds=50,
                            evals_result=evals_result,
                            verbose_eval=False)
            # print('evals_result = ', evals_result)
            prediction_probs = ConfidenceEstimator._extract_confidence_scores(model, dev_dataset)
            predictions = np.round(np.asarray(prediction_probs))
            accuracy = accuracy_score(dev_labels, predictions)
            score = model.best_score #evals_result['dev']['aucpr'][-1]#
            logger.info('score=%.3f \t accuracy=%.1f \t best_iteration=%d \t', score, accuracy * 100, model.best_iteration)
            confusion_m = confusion_matrix(dev_labels, predictions)
            if score > best_score:
                best_score = score
                best_model = model
                best_confusion_matrix = confusion_m
                best_params = m, e, n
            best_score = max(best_score, score)

            self.model = best_model
            self.score = best_score

        return best_model, best_score, best_confusion_matrix, best_params

    @staticmethod
    def convert_to_labels(confidences: Iterable[ConfidenceOutput]):
        labels = []
        for c in confidences:
            labels.append(c[0].first_mistake)
        labels = np.array(labels) + 1 # +1 so that minimum is 0
        labels = (labels == 0) # convert to binary labels
        # logger.info('labels = %s', str(labels))
        return labels

    def convert_to_dataset(self, confidences: Iterable[ConfidenceOutput], train :bool):
        labels = ConfidenceEstimator.convert_to_labels(confidences)
        features = self._convert_to_features(confidences, train)

        return features, labels

    def estimate(self, confidences: Iterable[ConfidenceOutput]):
        features, labels = self.convert_to_dataset(confidences, train=False)
        dataset = xgb.DMatrix(data=features, label=labels)
        confidence_scores = ConfidenceEstimator._extract_confidence_scores(self.model, dataset)
        return confidence_scores

    def evaluate(self, dev_features, dev_labels):
        dev_dataset = xgb.DMatrix(data=dev_features, label=dev_labels)
        confidence_scores = ConfidenceEstimator._extract_confidence_scores(self.model, dev_dataset)

        # order = range(len(dev_labels))
        # sorted_confidence_scores, sorted_labels, original_order = list(zip(*sorted(zip(confidence_scores, dev_labels, order))))
        # sorted_features = [dev_features[i] for i in original_order]
        # print('sorted_features = ', sorted_features[-6:-4])
        # print('sorted_confidence_scores = ',  sorted_confidence_scores[-6:-4])
        # print('sorted_confidence_scores = ',  sorted_confidence_scores)
        # print('sorted_labels = ', sorted_labels[-6:-4])
        
        precision, recall, thresholds = precision_recall_curve(dev_labels, confidence_scores)
        pass_rate, accuracies = accuracy_at_pass_rate(dev_labels, confidence_scores)

        return precision, recall, pass_rate, accuracies, thresholds

    def train_and_validate(self, train_features, train_labels, dev_features, dev_labels):
        train_dataset = xgb.DMatrix(data=train_features, label=train_labels)
        dev_dataset = xgb.DMatrix(data=dev_features, label=dev_labels)
        scale_pos_weight = np.sum(dev_labels)/(np.sum(1-dev_labels)) # 1s over 0s
        # logger.info('scale_pos_weight = %f', scale_pos_weight)

        best_model, best_score, best_confusion_matrix, best_params = self._tune_and_train(train_dataset=train_dataset, dev_dataset=dev_dataset, dev_labels=dev_labels, scale_pos_weight=scale_pos_weight)
        logger.info('best dev set score = %.3f', best_score)
        logger.info('best confusion_matrix = %s', str(best_confusion_matrix))
        logger.info('best hyperparameters (max_depth, eta, num_iterations) = %s', str(best_params))

    def save(self, path: str):
        with open(path, 'wb') as f:
            dill.dump(self, f, protocol=4)

    @staticmethod
    def load(path: str):
        with open(path, 'rb') as f:
            obj = dill.load(f)
        return obj

def main(args):
    if args.plot:
        from matplotlib import pyplot # lazy import

    with open(args.confidence_path, 'rb') as f:
        confidences = pickle.load(f)

    all_estimators = []
    train_confidences, dev_confidences = train_test_split(confidences, test_size=args.dev_split, random_state=args.seed)
    for f, name in [
                    # (oracle_score, 'raw_oracle'),
                    # (nodrop_avg_logprob(0), 'raw_avg_logprob'),
                    # (nodrop_seq_prob(0), 'raw_seq_prob'),
                    (mean_drop_seq_prob(0), 'raw_mean_seq_prob'),
                    (neg_of(var_drop_seq_prob(0)), 'raw_var_seq_prob'),
                    # (neg_of(cv_drop_seq_prob(0)), 'raw_cv_seq_prob'),
                    # (cev_drop_seq_prob(0), 'raw_cev_drop_seq_prob'),
                    # ([mean_drop_prob(0)], 'mean_prob'),
                    # ([var_drop_prob(0)], 'var_prob'),
                    # ([cev_drop_prob(0)], 'cev_drop_prob'),
                    # ([mean_drop_logit(0)], 'mean'),
                    # ([nodrop_entropies(0)], 'entropy'),
                    # ([(mean_drop_logit(0), nodrop_entropies(0))], 'mean + entropy'),
                    # ([prediction_length(0), (mean_drop_logit(0), nodrop_entropies(0))], 'prediction_length + mean + entropy'),
                    # ([prediction_length(0), (mean_drop_logit(0), nodrop_entropies(0), cv_drop_logit(0))], 'prediction_length + mean + entropy + cv'),
                    # ([max_of(nodrop_logit(0)), max_of(nodrop_entropies(0)), max_of(cv_drop_logit(0))], 'max_logit + max_entropy + max_cv'),
                    # ([prediction_length(0), max_of(nodrop_logit(0)), max_of(nodrop_entropies(0)), max_of(cv_drop_logit(0))], 'prediction_length + max_logit + max_entropy + max_cv'),
                    # ([prediction_length(0), max_of(nodrop_logit(0)), max_of(nodrop_entropies(0)), max_of(cv_drop_logit(0)), max_of(var_drop_logit(0)), min_of(nodrop_logit(0)), min_of(nodrop_entropies(0)), min_of(cv_drop_logit(0)), min_of(var_drop_logit(0))], 'prediction_length + max_logit + max_entropy + max_cv + max_var + min_logit + min_entropy + min_cv + min_var'),
                    # ([prediction_length(0), max_of(nodrop_logit(0)), max_of(nodrop_entropies(0)), max_of(cv_drop_logit(0)), min_of(nodrop_logit(0)), min_of(nodrop_entropies(0)), min_of(cv_drop_logit(0))], 'prediction_length + max_logit + max_entropy + max_cv + min_logit + min_entropy + min_cv'),
                    # ([prediction_length(0), max_of(nodrop_logit(0)), max_of(nodrop_entropies(0)), max_of(cv_drop_logit(0)), min_of(nodrop_logit(0)), min_of(nodrop_entropies(0)), min_of(cv_drop_logit(0)), input_length(0)], 'prediction_length + max_logit + max_entropy + max_cv + min_logit + min_entropy + min_cv + input_length'),
                    # ([nodrop_avg_logprob(0), prediction_length(0), max_of(nodrop_logit(0)), max_of(nodrop_entropies(0)), max_of(cv_drop_logit(0)), min_of(nodrop_logit(0)), min_of(nodrop_entropies(0)), min_of(cv_drop_logit(0)), input_length(0)], 'logprob + prediction_length + max_logit + max_entropy + max_cv + min_logit + min_entropy + min_cv + input_length'),
                    # ([nodrop_avg_logprob(0), prediction_length(0), max_of(nodrop_logit(0)), max_of(nodrop_entropies(0)), max_of(cv_drop_logit(0)), min_of(nodrop_logit(0)), input_length(0)], 'logprob + prediction_length + max_logit + max_entropy + max_cv + min_logit + input_length'),
                    ([nodrop_avg_logprob(0), prediction_length(0), max_of(nodrop_logit(0)), max_of(nodrop_entropies(0)), max_of(cv_drop_logit(0)), min_of(nodrop_logit(0)), input_length(0), cev_drop_seq_prob(0), mean_drop_seq_prob(0)], 'logprob + prediction_length + max_logit + max_entropy + max_cv + min_logit + input_length + cev_seq_prob + mean_seq_prob'),
                    # ([nodrop_seq_prob(0), prediction_length(0), max_of(mean_drop_prob(0)), max_of(nodrop_entropies(0)), max_of(cev_drop_prob(0)), min_of(mean_drop_prob(0)), input_length(0), cev_drop_seq_prob(0), mean_drop_seq_prob(0)], 'prob + prediction_length + max_logit + max_entropy + max_cv + min_logit + input_length + cev_seq_prob + mean_seq_prob'),
                    # ([variance_of_beam_logits], 'var_beam_logits'),
                    # ([variance_of_beam_probs], 'var_beam_probs'),
                    # ([mean_drop_avg_logprob(0)], 'mean_drop_avg_logprob'),
                    # ([var_drop_avg_logprob(0)], 'var_drop_avg_logprob'),
                    # ([cv_drop_avg_logprob(0)], 'cv_drop_avg_logprob'),

                    ]:
        estimator = ConfidenceEstimator(name=name, featurizers=f, eval_metric=args.eval_metric)
        logger.info('name = %s', name)
        
        if name.startswith('raw'):
            precision, recall, pass_rate, accuracies, thresholds = evaluate_raw(dev_confidences, f)
            score = auc(recall, precision)
            estimator.score = score
            logger.info('dev set score = %.3f', score)
        else:
            train_features, train_labels = estimator.convert_to_dataset(train_confidences, train=True)
            dev_features, dev_labels = estimator.convert_to_dataset(dev_confidences, train=False)
            estimator.train_and_validate(train_features, train_labels, dev_features, dev_labels)
            precision, recall, pass_rate, accuracies, thresholds = estimator.evaluate(dev_features, dev_labels)
        if args.plot:
            pyplot.figure('precision-recall')
            pyplot.plot(recall, precision, marker='.', label=name)
            pyplot.figure('thresholds')
            pyplot.plot(range(len(thresholds)), thresholds, marker='*', label=name+ ' (thresholds)')
            pyplot.figure('pass_rate')
            pyplot.plot(pass_rate, accuracies, marker='.', label=name)

        all_estimators.append(estimator)
        
    logger.info('\n'+'\n'.join([f'{e.name}: {e.score:.3f}' for e in all_estimators]))
    best_estimator = all_estimators[np.argmax([e.score for e in all_estimators])]
    logger.info('Best estimator is %s with score = %.3f', best_estimator.name, best_estimator.score)
    best_estimator.save(args.save)

    if args.plot:
        pyplot.figure('precision-recall')
        pyplot.legend(prop={'size': 6})
        pyplot.grid()
        pyplot.xticks(np.arange(0, 1, 0.1))
        pyplot.xlim(0, 1)
        pyplot.xlabel('Recall')
        pyplot.ylabel('Precision')
        pyplot.savefig('precision-recall.png')

        pyplot.figure('thresholds')
        pyplot.legend(prop={'size': 6})
        pyplot.grid()
        pyplot.xlabel('Index')
        pyplot.ylabel('Confidence Threshold')
        pyplot.savefig('threshold.png')

        pyplot.figure('pass_rate')
        pyplot.legend(prop={'size': 6})
        pyplot.grid()
        pyplot.xticks(np.arange(0, 1, 0.1))
        pyplot.xlim(0, 1)
        pyplot.xlabel('Pass Rate')
        pyplot.ylabel('Accuracy')
        pyplot.savefig('pass-accuracy.png')
    