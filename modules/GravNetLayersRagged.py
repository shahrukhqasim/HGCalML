import tensorflow as tf
from caloGraphNN import gauss_of_lin
from select_knn_op import SelectKnn
from accknn_op import AccumulateKnn
from local_cluster_op import LocalCluster

#just for the moment
from index_dicts import create_truth_dict
#### helper###


def check_type_return_shape(s):
    if not isinstance(s, tf.TensorSpec):
        raise TypeError('Only TensorSpec signature types are supported, '
                      'but saw signature entry: {}.'.format(s))
    return s.shape


############# Some layers for convenience ############

class ExtractTruthContributions(tf.keras.layers.Layer):
    def __init__(self,
                 **kwargs):
        """
        Inputs are: 
         - Full truth array
         
        Call will return:
         - truth association indices (as float right now (FIXME, with other changes downstream))
         - truth energy
         - truth position (x,y,t)
         - truth classes (not as one-hot)
        
        no parameters.
        
        """
        super(ExtractTruthContributions, self).__init__(**kwargs)
        
    def compute_output_shape(self, input_shapes):
        return (1,), (1,), (3,), (1,)
    
    def call(self, input):
        d = create_truth_dict(input)
        idx = d['truthHitAssignementIdx']
        e = d['truthHitAssignedEnergies']
        x = d['truthHitAssignedX']
        return d,idx,e,x

############# Local clustering section



class LocalClustering(tf.keras.layers.Layer):
    def __init__(self,
                 print_reduction=False,
                 **kwargs):
        """
        Inputs are: 
         - neighbour indices (V x K)
         - hierarchy tensor (V x 1) to determine the clustering hierarchy
         - row splits
         
        Call will return:
         - indices to select the cluster centres, 
         - updated row splits for after applying the selection
         - indices to gather back the original dimensionality by repition
        
        no parameters.
        
        """
        super(LocalClustering, self).__init__(dynamic=False,**kwargs)
        self.print_reduction=print_reduction
        
    def get_config(self):
        config = {'print_reduction': self.print_reduction}
        base_config = super(LocalClustering, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))
        
    def compute_output_shape(self, input_shapes):
        return (None,1), (None,), (None,1)
    
    
    def compute_output_signature(self, input_signature):
        
        input_shapes = [x.shape for x in input_spec]
        output_shapes = self.compute_output_shape(input_shapes)

        return [tf.TensorSpec(dtype=tf.int32, shape=output_shapes[i]) for i in range(len(output_shape))]
    
   
    def build(self, input_shapes):
        super(LocalClustering, self).build(input_shapes)
        
    def call(self, inputs):
        neighs, hier, row_splits = inputs
        
        if row_splits.shape[0] is None:
            return tf.zeros_like(hier, dtype='int32'), row_splits, tf.zeros_like(hier, dtype='int32')
        
        if hier.shape[1] > 1:
            raise ValueError(self.name+' received wrong hierarchy shape')
        
        hierarchy_idxs=[]
        for i in range(row_splits.shape[0] - 1):
            a = tf.argsort(hier[row_splits[i]:row_splits[i+1]],axis=0, direction='DESCENDING')
            hierarchy_idxs.append(a+row_splits[i])
        hierarchy_idxs = tf.concat(hierarchy_idxs,axis=0)
        
        
        rs,sel,ggather = LocalCluster(neighs, hierarchy_idxs, row_splits)
        
        #keras does not like gather_nd outputs
        sel = tf.reshape(sel, [-1,1])
        rs = tf.reshape(rs, [-1])
        ggather = tf.reshape(ggather, [-1,1])
        if self.print_reduction:
            tf.print(self.name,'reduction',float(sel.shape[0])/float(ggather.shape[0]),'to',sel.shape[0])
        return sel, rs, ggather
        
class CreateGlobalIndices(tf.keras.layers.Layer):
    def __init__(self, **kwargs):    
        """
        Inputs are:
         - a tensor to determine the total dimensionality in the first dimension
        """  
        super(CreateGlobalIndices, self).__init__(dynamic=False,**kwargs)
        
    def compute_output_shape(self, input_shape):
        s = (input_shape[0],1)
        return s
    
    
    def compute_output_signature(self, input_signature):
        print('>>>>>CreateGlobalIndices input_signature',input_signature)
        input_shape = tf.nest.map_structure(check_type_return_shape, input_signature)
        output_shape = self.compute_output_shape(input_shape)
        return [tf.TensorSpec(dtype=tf.int32, shape=output_shape[i]) for i in range(len(output_shape))]
    
    def build(self, input_shapes):
        super(CreateGlobalIndices, self).build(input_shapes)
    
    def call(self, input):
        ins = tf.cast(input*0.,dtype='int32')[:,0:1]
        add = tf.expand_dims(tf.range(tf.shape(input)[0],dtype='int32'), axis=1)
        return ins+add
    
    
class SelectFromIndices(tf.keras.layers.Layer): 
    def __init__(self, **kwargs):    
        """
        Inputs are:
         - the selection indices
         - a list of tensors the selection should be applied to (extending the indices)
        """  
        super(SelectFromIndices, self).__init__(dynamic=False,**kwargs) 
        
    def compute_output_shape(self, input_shapes):#these are tensors shapes
        ts = tf.python.framework.tensor_shape.TensorShape
        outshapes = [ts([None, ] +s.as_list()[1:]) for s in input_shapes][1:]
        return outshapes #all but first (indices)
    
    
    def compute_output_signature(self, input_signature):
        print('>>>>>SelectFromIndices input_signature',input_signature)
        input_shape = tf.nest.map_structure(check_type_return_shape, input_signature)
        output_shape = self.compute_output_shape(input_shape)
        input_dtypes=[i.dtype for i in input_signature]
        return [tf.TensorSpec(dtype=input_dtypes[i+1], shape=output_shape[i]) for i in range(0,len(output_shape))]
    
    def build(self, input_shapes):
        super(SelectFromIndices, self).build(input_shapes)
        self.outshapes = self.compute_output_shape(input_shapes)
          
    def call(self, inputs):
        indices = inputs[0]
        outs=[]
        #outshapes = self.compute_output_shape([tf.shape(i) for i in inputs])
        for i in range(1,len(inputs)):
            g = tf.gather_nd( inputs[i], indices)
            outs.append(g) 
        return outs
        
class MultiBackGather(tf.keras.layers.Layer):  
    def __init__(self, **kwargs):    
        """
        Inputs are:
         - the data to gather back to larger dimensionality by repitition
        """  
        self.gathers=[]
        super(MultiBackGather, self).__init__(dynamic=False,**kwargs) 
        
    def compute_output_shape(self, input_shape):
        return input_shape #batch dim is None anyway
    
    def append(self, idxs):
        self.gathers.append(idxs)
        
    def call(self, input):
        sel_gidx, gathers = input
        for k in range(len(gathers)):
            l = len(self.gathers) - k - 1
            sel_gidx = SelectFromIndices()([ gathers[l], sel_gidx] )[0]
        return sel_gidx
    
############# Local clustering section ends


class KNN(tf.keras.layers.Layer):
    def __init__(self,K: int, radius: float, **kwargs):
        """
        Call will return 
         - self + K neighbour indices of K neighbours within max radius
         - distances to self+K neighbours
        
        Inputs: coordinates, row_splits
        
        :param K: number of nearest neighbours
        :param radius: maximum distance of nearest neighbours
        """
        super(KNN, self).__init__(**kwargs) 
        self.K = K
        self.radius = radius
        
        
    def get_config(self):
        config = {'K': self.K,
                  'radius': self.radius}
        base_config = super(KNN, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

    def compute_output_shape(self, input_shapes):
        return (None, self.K+1),(None, self.K+1)

    def call(self, inputs):
        coordinates, row_splits = inputs
        idx,dist = SelectKnn(self.K+1, coordinates,  row_splits,
                             max_radius= self.radius, tf_compatible=False)

        idx = tf.reshape(idx, [-1,self.K+1])
        dist = tf.reshape(dist, [-1,self.K+1])
        return idx,dist

######## generic neighbours

class RaggedGravNet(tf.keras.layers.Layer):
    def __init__(self,
                 n_neighbours: int,
                 n_dimensions: int,
                 n_filters : int,
                 n_propagate : int,
                 **kwargs):
        """
        Call will return output features, coordinates, neighbor indices and squared distances from neighbors

        :param n_neighbours: neighbors to do gravnet pass over
        :param n_dimensions: number of dimensions in spatial transformations
        :param n_filters:  number of dimensions in output feature transformation, could be list if multiple output
        features transformations (minimum 1)

        :param n_propagate: how much to propagate in feature tranformation, could be a list in case of multiple
        :param kwargs:
        """
        super(RaggedGravNet, self).__init__(**kwargs)

        n_neighbours += 1  # includes the 'self' vertex
        assert n_neighbours > 1

        self.n_neighbours = n_neighbours
        self.n_dimensions = n_dimensions
        self.n_filters = n_filters

        self.n_propagate = n_propagate
        self.n_prop_total = 2 * self.n_propagate

        with tf.name_scope(self.name + "/1/"):
                self.input_feature_transform = tf.keras.layers.Dense(n_propagate, activation='relu')

        with tf.name_scope(self.name + "/2/"):
            self.input_spatial_transform = tf.keras.layers.Dense(n_dimensions)

        with tf.name_scope(self.name + "/3/"):
            self.output_feature_transform = tf.keras.layers.Dense(self.n_filters, activation='tanh')

    def build(self, input_shapes):
        input_shape = input_shapes[0]

        with tf.name_scope(self.name + "/1/"):
            self.input_feature_transform.build(input_shape)

        with tf.name_scope(self.name + "/2/"):
            self.input_spatial_transform.build(input_shape)

        with tf.name_scope(self.name + "/3/"):
            self.output_feature_transform.build((input_shape[0], self.n_prop_total + input_shape[1]))

        super(RaggedGravNet, self).build(input_shape)

    def create_output_features(self, x, neighbour_indices, distancesq):
        allfeat = []
        features = x

        features = self.input_feature_transform(features)
        prev_feat = features
        features = self.collect_neighbours(features, neighbour_indices, distancesq)
        features = tf.reshape(features, [-1, prev_feat.shape[1] * 2])
        features -= tf.tile(prev_feat, [1, 2])
        allfeat.append(features)

        features = tf.concat(allfeat + [x], axis=-1)
        return self.output_feature_transform(features)

    def priv_call(self, inputs):
        x = inputs[0]
        row_splits = inputs[1]
        
        coordinates = self.input_spatial_transform(x)

        neighbour_indices, distancesq = self.compute_neighbours_and_distancesq(coordinates, row_splits)

        return self.create_output_features(x, neighbour_indices, distancesq), coordinates, neighbour_indices, distancesq

    def call(self, inputs):
        return self.priv_call(inputs)

    def compute_output_shape(self, input_shapes):
        return (input_shapes[0], self.n_filters)

    def compute_neighbours_and_distancesq(self, coordinates, row_splits):
        #
        # ragged_split_added_indices, _ = SelectKnn(self.n_neighbours, coordinates, row_splits,
        #                                           max_radius=1.0, tf_compatible=True)
        #
        # # ragged_split_added_indices = ragged_split_added_indices[:,1:]
        #
        # ragged_split_added_indices = ragged_split_added_indices[..., tf.newaxis]
        #
        # distancesq = tf.reduce_sum(
        #     (coordinates[:, tf.newaxis, :] - tf.gather_nd(coordinates, ragged_split_added_indices)) ** 2,
        #     axis=-1)  # [SV, N]
        idx,dist = SelectKnn(self.n_neighbours, coordinates,  row_splits,
                             max_radius= -1.0, tf_compatible=False)

        idx = idx[:, 1:]
        dist = dist[:, 1:]

        return idx,dist


        return idx, distancesq

    def collect_neighbours(self, features, neighbour_indices, distancesq):

        f,_ = AccumulateKnn(10.*distancesq,  features, neighbour_indices, n_moments=0)
        return f

    def get_config(self):
        config = {'n_neighbours': self.n_neighbours,
                  'n_dimensions': self.n_dimensions,
                  'n_filters': self.n_filters,
                  'n_propagate': self.n_propagate}
        base_config = super(RaggedGravNet, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))



class DynamicDistanceMessagePassing(tf.keras.layers.Layer):
    '''
    allows distances after each passing operation to be dynamically adjusted.
    this similar to FusedRaggedGravNetAggAtt, but incorporates the scaling in the message passing loop
    '''

    def __init__(self, n_feature_transformation,
                 **kwargs):
        super(DynamicDistanceMessagePassing, self).__init__(**kwargs)

        self.dist_mod_dense = []
        self.n_feature_transformation = n_feature_transformation
        self.feature_tranformation_dense = []
        for i in range(len(self.n_feature_transformation)):
            with tf.name_scope(self.name + "/5/" + str(i)):
                self.dist_mod_dense.append(tf.keras.layers.Dense(1, activation='sigmoid'))  # restrict variations a bit
            with tf.name_scope(self.name + "/6/" + str(i)):
                self.feature_tranformation_dense.append(tf.keras.layers.Dense(self.n_feature_transformation[i], activation='relu'))

    def build(self, input_shapes):
        input_shape = input_shapes[0]

        with tf.name_scope(self.name + "/5/" + str(0)):
            self.dist_mod_dense[0].build((input_shape[0], input_shape[1]))
        for i in range(1, len(self.dist_mod_dense)):
            with tf.name_scope(self.name+"/5/"+str(i)):
                self.dist_mod_dense[i].build((input_shape[0],input_shape[1]+self.n_feature_transformation[i-1]*2))

        with tf.name_scope(self.name + "/6/" + str(0)):
            self.feature_tranformation_dense[0].build(input_shape)
        for i in range(1, len(self.dist_mod_dense)):
            with tf.name_scope(self.name + "/6/" + str(i)):
                self.feature_tranformation_dense[i].build((input_shape[0], self.n_feature_transformation[i-1] * 2))


        super(DynamicDistanceMessagePassing, self).build(input_shapes)


    def create_output_features(self, x, neighbour_indices, distancesq):
        allfeat = []
        features = x

        for i in range(len(self.n_feature_transformation)):
            if i == 0:
                scale = 10. * self.dist_mod_dense[0](x)
            else:
                scale = 10. * self.dist_mod_dense[i](tf.concat([x, features], axis=-1))
            distancesq *= scale
            t = self.feature_tranformation_dense[i]
            features = t(features)
            prev_feat = features
            features = self.collect_neighbours(features, neighbour_indices, distancesq)

            features = tf.reshape(features, [-1, prev_feat.shape[1] * 2])
            features -= tf.tile(prev_feat, [1, 2])

            allfeat.append(features)

        features = tf.concat(allfeat + [x], axis=-1)
        return features

    def collect_neighbours(self, features, neighbour_indices, distancesq):
        f,_ = AccumulateKnn(10.*distancesq,  features, neighbour_indices, n_moments=0)
        return f

    def call(self, inputs):
        x, neighbor_indices, distancesq = inputs
        return self.create_output_features(x, neighbor_indices, distancesq)


    def get_config(self):
        config = {
                  'n_feature_transformation': self.n_feature_transformation}
        base_config = super(DynamicDistanceMessagePassing, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class CollectNeighbourAverageAndMax(tf.keras.layers.Layer):
    def __init__(self,**kwargs):
        '''
        Simply accumulates all neighbour index information (including self if in the neighbour indices)
        Output will be divded by K, but not explicitly averaged if the number of neighbours is <K for
        particular vertices
        
        Inputs:  data, idxs
        '''
        super(CollectNeighbourAverageAndMax, self).__init__(**kwargs)

    def compute_output_shape(self, input_shapes): # data, idxs
        return (input_shapes[0][0],2*input_shapes[0][-1])
    
    def call(self, inputs):
        x, idxs = inputs
        f,_ = AccumulateKnn(tf.cast(idxs*0, tf.float32),  x, idxs, n_moments=0)
        return tf.reshape(f, [-1,2*x.shape[-1]])
    

class MessagePassing(tf.keras.layers.Layer):
    '''
    Inputs: x, neighbor_indices
    
    
    '''

    def __init__(self, n_feature_transformation,
                 **kwargs):
        super(MessagePassing, self).__init__(**kwargs)

        self.n_feature_transformation = n_feature_transformation
        self.feature_tranformation_dense = []
        for i in range(len(self.n_feature_transformation)):
            with tf.name_scope(self.name + "/5/" + str(i)):
                self.feature_tranformation_dense.append(tf.keras.layers.Dense(self.n_feature_transformation[i], activation='relu'))  # restrict variations a bit

    def build(self, input_shapes):
        input_shape = input_shapes[0]

        with tf.name_scope(self.name + "/5/" + str(0)):
            self.feature_tranformation_dense[0].build(input_shape)

        for i in range(1, len(self.feature_tranformation_dense)):
            with tf.name_scope(self.name + "/5/" + str(i)):
                self.feature_tranformation_dense[i].build((input_shape[0], self.n_feature_transformation[i-1] * 2))

        super(MessagePassing, self).build(input_shapes)

    def create_output_features(self, x, neighbour_indices):
        allfeat = []
        features = x


        for i in range(len(self.n_feature_transformation)):
            t = self.feature_tranformation_dense[i]
            features = t(features)
            prev_feat = features
            features = self.collect_neighbours(features, neighbour_indices)
            features = tf.reshape(features, [-1, prev_feat.shape[1] * 2])
            features -= tf.tile(prev_feat, [1, 2])
            allfeat.append(features)

        features = tf.concat(allfeat + [x], axis=-1)
        return features

    def collect_neighbours(self, features, neighbour_indices):
        f,_ = AccumulateKnn(tf.cast(neighbour_indices*0, tf.float32),  features, neighbour_indices, n_moments=0)
        return f

    def call(self, inputs):
        x, neighbor_indices = inputs
        return self.create_output_features(x, neighbor_indices)

    def get_config(self):
        config = {'n_feature_transformation': self.n_feature_transformation,
                  }
        base_config = super(MessagePassing, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))


class DistanceWeightedMessagePassing(tf.keras.layers.Layer):
    '''

    '''

    def __init__(self, n_feature_transformation,
                 **kwargs):
        super(DistanceWeightedMessagePassing, self).__init__(**kwargs)

        self.n_feature_transformation = n_feature_transformation
        self.feature_tranformation_dense = []
        for i in range(len(self.n_feature_transformation)):
            with tf.name_scope(self.name + "/5/" + str(i)):
                self.feature_tranformation_dense.append(tf.keras.layers.Dense(self.n_feature_transformation[i],
                                                                              activation='relu'))  # restrict variations a bit

    def build(self, input_shapes):
        input_shape = input_shapes[0]

        with tf.name_scope(self.name + "/5/" + str(0)):
            self.feature_tranformation_dense[0].build(input_shape)

        for i in range(1, len(self.feature_tranformation_dense)):
            with tf.name_scope(self.name + "/5/" + str(i)):
                self.feature_tranformation_dense[i].build((input_shape[0], self.n_feature_transformation[i - 1] * 2))

        super(DistanceWeightedMessagePassing, self).build(input_shapes)


    def get_config(self):
        config = {'n_feature_transformation': self.n_feature_transformation,
        }
        base_config = super(DistanceWeightedMessagePassing, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))

    def create_output_features(self, x, neighbour_indices, distancesq):
        allfeat = []
        features = x

        for i in range(len(self.n_feature_transformation)):
            t = self.feature_tranformation_dense[i]
            features = t(features)
            prev_feat = features
            features = self.collect_neighbours(features, neighbour_indices, distancesq)
            features = tf.reshape(features, [-1, prev_feat.shape[1] * 2])
            features -= tf.tile(prev_feat, [1, 2])
            allfeat.append(features)

        features = tf.concat(allfeat + [x], axis=-1)
        return features

    def collect_neighbours(self, features, neighbour_indices, distancesq):

        # weights = gauss_of_lin(10. * distancesq)
        # weights = tf.expand_dims(weights, axis=-1)  # [SV, N, 1]
        # neighbour_features = tf.gather_nd(features, neighbour_indices)
        # neighbour_features *= weights
        # neighbours_max = tf.reduce_max(neighbour_features, axis=1)
        # neighbours_mean = tf.reduce_mean(neighbour_features, axis=1)
        #
        f,_ = AccumulateKnn(10.*distancesq,  features, neighbour_indices, n_moments=0)
        return f


        return tf.concat([neighbours_max, neighbours_mean], axis=-1)



    def call(self, inputs):
        x, neighbor_indices, distancesq = inputs
        return self.create_output_features(x, neighbor_indices, distancesq)


    def get_config(self):
        config = {'n_feature_transformation': self.n_feature_transformation,
                  }
        base_config = super(DistanceWeightedMessagePassing, self).get_config()
        return dict(list(base_config.items()) + list(config.items()))
