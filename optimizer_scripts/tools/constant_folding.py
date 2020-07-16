import onnx.utils
import onnx
import numpy as np
import logging

from . import helper
from .general_graph import Graph, Node
from .other import topological_sort
from .replacing import replace_shape_with_constant

def are_all_inputs_Constant_with_one_child(g, node):
    for input_name in node.input:
        input_node = helper.find_node_by_output_name(g, input_name)
        if input_node is None or input_node.op_type != 'Constant':
            return False
        relative_outputs = helper.find_nodes_by_input_name(g, input_name)
        if len(relative_outputs) > 1:
            return False
    return True

def constant_folding(g):
    # Before constant folding, duplicate the constant nodes.
    duplicate_constant_node(g)
    keep_folding = True
    folded = False
    while keep_folding:
        keep_folding = False
        for node in g.node:
            # Check if the node is foldable
            if node.op_type not in constant_folding_nodes.keys():
                continue
            # Check if the parents of the node are all single follower constant node.
            if not are_all_inputs_Constant_with_one_child(g, node):
                continue
            # Constant folding for the specific node
            if constant_folding_nodes[node.op_type](g):
                logging.debug("Constant nodes and %s %s are folded.", node.op_type, node.name)
                folded = True
                keep_folding = True
            else:
                logging.debug("Constant nodes and %s %s are skipped.", node.op_type, node.name)
    return folded


def duplicate_constant_node(g):
    """ Duplicate the constant node if its following nodes contain constant folding
    nodes. Create and link the new constant nodes to the constant folding nodes.
    """
    for node in g.node:
        # Find a valid constant node
        if node.op_type != 'Constant':
            continue
        output_val_info = helper.find_value_by_name(g, node.output[0])
        data_shape = helper.get_shape_from_value_info(output_val_info)
        output_nodes = helper.find_nodes_by_input_name(g, node.output[0])

        # For constant that has only one following node, no need to duplicate
        if len(output_nodes) < 2:
            continue

        # Check if its following nodes are foldable
        foldable_output_nodes = list(filter(lambda n: n.op_type in
                                            constant_folding_nodes.keys(), output_nodes))
        if not foldable_output_nodes:
            continue

        # Duplicate the node needed by foldable nodes
        for i in range(len(foldable_output_nodes)):
            logging.debug("Found constant %s and %s %s are availble for folding. Duplicate constant.",
                          node.name, foldable_output_nodes[i].op_type, foldable_output_nodes[i].name)
            output_name = node.output[0] + '_dup_' + str(i)
            new_constant_node = onnx.helper.make_node(
                'Constant',
                [],
                [output_name],
                name=output_name,
                value=node.attribute[0].t
            )
            new_val_info = onnx.helper.make_tensor_value_info(
                output_name,
                node.attribute[0].t.data_type,
                data_shape
            )
            input_ind = list(foldable_output_nodes[i].input).index(
                node.output[0])
            foldable_output_nodes[i].input[input_ind] = output_name

            g.node.extend([new_constant_node])
            g.value_info.extend([new_val_info])

        # If all following nodes are foldable node, delete the original node.
        if len(foldable_output_nodes) == len(output_nodes):
            g.node.remove(node)
            g.value_info.remove(output_val_info)

    topological_sort(g)

    return

def unsqueeze_constant_folding(g):
    """Do Unsqueeze layer constant folding for the pytorch model.

    :param g: the input graph
    :return: None
    """
    graph = Graph(g)
    todo = graph.get_sorted_node_list()
    node_to_remove = []
    folded = False
    for node in todo:
        if node.proto is None:
            continue
        if node.proto.op_type != 'Unsqueeze':
            continue
        if not helper.all_constant_input(node):
            continue
        # Now we have an unsqueeze to fold.
        # Find the previous constant node.
        prev_data_node = node.parents[0]
        prev_data = helper.constant_to_numpy(prev_data_node.proto)
        new_dims = helper.get_attribute_by_name(node.proto, 'axes')
        new_dims = new_dims.ints
        data = prev_data
        for d in new_dims:
            data = np.expand_dims(data, d)
        # Construct new node
        new_const = helper.numpy_to_constant(
            node.proto.output[0], data)
        # Modify graph proto
        g.node.extend([new_const])
        if node.proto not in node_to_remove:
            node_to_remove.append(node.proto)
        # Modify Graph structure
        const_node = Node(new_const)
        for next_node in node.children:
            next_node.parents = [
                prev if prev != node else const_node for prev in next_node.parents]
            const_node.children.append(next_node)
        folded = True
    for node in node_to_remove:
        g.node.remove(node)
    return folded


def slice_constant_folding(g):
    """ Fold constant and slice nodes to a single constant node.
    """
    node_to_delete = []
    folded = False
    for node in g.node:
        if node.op_type != 'Slice':
            continue
        pre_node = helper.find_node_by_output_name(g, node.input[0])
        if pre_node.op_type != 'Constant':
            continue
        pre_shape, data_list = helper.constant_to_list(pre_node)

        data_list = np.reshape(data_list, pre_shape)
        axes = helper.get_attribute_by_name(node, 'axes')
        ends = list(helper.get_attribute_by_name(node, 'ends').ints)
        starts = list(helper.get_attribute_by_name(node, 'starts').ints)

        if not axes:
            axes = list(range(len(helper.get_shape(data_list))))
        else:
            axes = list(axes.ints)

        new_data = helper.slice_data(data_list, starts, ends, axes)
        new_node = helper.list_to_constant(node.output[0], helper.get_shape(
            new_data), helper.flatten_to_list(new_data))
        g.node.extend([new_node])
        node_to_delete.append(node)
        node_to_delete.append(pre_node)
        value_info = helper.find_value_by_name(g, pre_node.output[0])
        g.value_info.remove(value_info)
        folded = True

    while node_to_delete:
        node = node_to_delete.pop()
        g.node.remove(node)

    topological_sort(g)
    return folded


def cast_constant_folding(g):
    """ Fold constant and cast node to a single constant node.
    """
    node_to_delete = []
    folded = False
    for node in g.node:
        if node.op_type != 'Cast':
            continue
        pre_node = helper.find_node_by_output_name(g, node.input[0])
        if not pre_node:
            continue
        if pre_node.op_type != 'Constant':
            continue

        shape, data = helper.constant_to_list(pre_node)
        data_type = node.attribute[0].i
        if data_type in (6, 7):
            data = list(map(int, data))
        elif data_type == onnx.helper.TensorProto.FLOAT:
            data = list(map(float, data))
        else:
            raise RuntimeError('data type not supported')

        if shape == 1:
            tensor = onnx.helper.make_tensor(
                name=pre_node.attribute[0].name,
                data_type=data_type,
                dims=[],
                vals=data
            )
        else:
            tensor = onnx.helper.make_tensor(
                name=pre_node.attribute[0].name,
                data_type=data_type,
                dims=shape,
                vals=helper.flatten_to_list(data)
            )
        new_node = onnx.helper.make_node(
            'Constant',
            [],
            [node.output[0]],
            name=node.output[0],
            value=tensor
        )
        g.node.extend([new_node])
        node_to_delete.append(pre_node)
        node_to_delete.append(node)

        value_info = helper.find_value_by_name(g, pre_node.output[0])
        g.value_info.remove(value_info)
        value_info = helper.find_value_by_name(g, node.output[0])
        g.value_info.remove(value_info)
        folded = True

    while node_to_delete:
        node = node_to_delete.pop()
        g.node.remove(node)

    topological_sort(g)
    return folded


def reduceprod_constant_folding(g):
    """ Fold constant and reduceprod nodes to a single constant node.
    """
    node_to_delete = []
    folded = False
    for node in g.node:
        if node.op_type != 'ReduceProd':
            continue
        pre_node = helper.find_node_by_output_name(g, node.input[0])
        if pre_node.op_type != 'Constant':
            continue

        shape, data_set = helper.constant_to_list(pre_node)
        tensor = pre_node.attribute[0].t

        data_set = np.reshape(data_set, shape)
        for att in node.attribute:
            if att.name == 'axes':
                axes = list(att.ints)
            else:
                keepdims = int(att.i)

        new_data = np.prod(data_set, axis=tuple(axes), keepdims=keepdims == 1)
        new_shape = helper.get_shape(new_data)
        new_flat_data = helper.flatten_to_list(new_data)
        new_tensor = onnx.helper.make_tensor(
            name=node.output[0],
            data_type=tensor.data_type,
            dims=new_shape,
            vals=new_flat_data
        )
        new_node = onnx.helper.make_node(
            'Constant',
            [],
            [node.output[0]],
            name=node.output[0],
            value=new_tensor
        )

        node_to_delete.extend([pre_node, node])
        g.node.extend([new_node])
        value_info = None
        for item in g.value_info:
            if item.name == pre_node.output[0]:
                value_info = item
        if value_info is not None:
            g.value_info.remove(value_info)
        folded = True

    while node_to_delete:
        node = node_to_delete.pop()
        g.node.remove(node)

    topological_sort(g)
    return folded


def reshape_constant_input_folding(g):
    """ Fold constant and reshape nodes to a single constant node.
    """
    node_to_delete = []
    folded = False
    for node in g.node:
        if node.op_type != 'Reshape':
            continue
        pre_data_node = helper.find_node_by_output_name(g, node.input[0])
        pre_shape_node = helper.find_node_by_output_name(g, node.input[1])
        if pre_data_node.op_type != 'Constant' or \
                pre_shape_node.op_type != 'Constant':
            continue
        if len(helper.find_nodes_by_input_name(g, pre_data_node.output[0])) > 1:
            continue
        if len(helper.find_nodes_by_input_name(g, pre_shape_node.output[0])) > 1:
            continue

        data = helper.constant_to_numpy(pre_data_node)
        _, shape = helper.constant_to_list(pre_shape_node)
        new_data = np.reshape(data, shape)

        new_tensor = onnx.helper.make_tensor(
            name=node.output[0],
            data_type=pre_data_node.attribute[0].t.data_type,
            dims=shape,
            vals=helper.flatten_to_list(new_data)
        )
        new_node = onnx.helper.make_node(
            'Constant',
            [],
            [node.output[0]],
            name=node.output[0],
            value=new_tensor
        )
        g.node.extend([new_node])

        node_to_delete.extend([node, pre_data_node, pre_shape_node])

        data_val_info = helper.find_value_by_name(g, pre_data_node.output[0])
        shape_val_info = helper.find_value_by_name(g, pre_shape_node.output[0])

        g.value_info.remove(data_val_info)
        g.value_info.remove(shape_val_info)
        folded = True

    while node_to_delete:
        node = node_to_delete.pop()
        g.node.remove(node)

    topological_sort(g)
    return folded


def concat_constant_folding(g):
    """ Fold constant and concat nodes to a single constant node.
    """
    node_to_del = []
    folded = False
    for node in g.node:
        if node.op_type != 'Concat':
            continue

        valid_inputs = True
        for input_name in node.input:
            input_node = helper.find_node_by_output_name(g, input_name)
            input_node_output = helper.find_nodes_by_input_name(g, input_name)
            if len(input_node_output) > 1:
                valid_inputs = False
                break
            if input_node.op_type != 'Constant':
                valid_inputs = False
                break

        if not valid_inputs:
            continue

        input_data = []
        input_shapes = []
        for input_name in node.input:
            input_node = helper.find_node_by_output_name(g, input_name)
            s, d = helper.constant_to_list(input_node)
            d = np.reshape(d, s)
            input_data.append(d)
            input_shapes.append(s)
            node_to_del.append(input_node)

        concat_data = np.concatenate(input_data, axis=node.attribute[0].i)
        new_node = helper.list_to_constant(
            node.output[0],
            helper.get_shape(concat_data),
            helper.flatten_to_list(concat_data),
            data_type=input_node.attribute[0].t.data_type
        )
        g.node.extend([new_node])
        node_to_del.append(node)

        for input_name in node.input:
            val_info = helper.find_value_by_name(g, input_name)
            g.value_info.remove(val_info)
        folded = True

    while node_to_del:
        node = node_to_del.pop()
        g.node.remove(node)

    topological_sort(g)
    return folded


def transpose_constant_folding(g):
    """Fold constant and transpose nodes to a single constant node.
    """
    node_to_del = []
    folded = False
    for node in g.node:
        if node.op_type != 'Transpose':
            continue
        pre_node = helper.find_node_by_output_name(g, node.input[0])
        if pre_node.op_type != 'Constant':
            continue
        shape, data = helper.constant_to_list(pre_node)
        np_data = np.reshape(data, shape)
        permutation = list(node.attribute[0].ints)

        new_data = np.transpose(np_data, permutation)
        new_shape = new_data.shape
        new_node = helper.list_to_constant(
            node.output[0],
            new_shape,
            new_data.flatten().tolist(),
            data_type=pre_node.attribute[0].t.data_type
        )

        g.node.extend([new_node])
        node_to_del.extend([node, pre_node])

        pre_val_info = helper.find_value_by_name(g, node.input[0])
        g.value_info.remove(pre_val_info)

        next_val_info = helper.find_value_by_name(g, node.output[0])
        g.value_info.remove(next_val_info)

        new_val_info = onnx.helper.make_tensor_value_info(
            node.output[0],
            pre_node.attribute[0].t.data_type,
            new_shape
        )
        g.value_info.extend([new_val_info])

    while node_to_del:
        node = node_to_del.pop()
        g.node.remove(node)
        folded = True

    topological_sort(g)
    return folded


def unsqueeze_constant_folding1(g):
    """Fold constant and unsqueeze nodes to a single constant node.
    """
    node_to_del = []
    folded = False
    for node in g.node:
        if node.op_type != 'Unsqueeze':
            continue
        pre_node = helper.find_node_by_output_name(g, node.input[0])
        if pre_node.op_type != 'Constant':
            continue
        shape, data = helper.constant_to_list(pre_node)
        if type(shape) == int:
            np_data = data[0]
        else:
            np_data = np.reshape(data, shape)
        axes = list(node.attribute[0].ints)
        axes.sort()

        for dim in axes:
            np_data = np.expand_dims(np_data, axis=dim)
        new_shape = np_data.shape
        new_node = helper.list_to_constant(
            node.output[0],
            new_shape,
            np_data.flatten().tolist(),
            data_type=pre_node.attribute[0].t.data_type
        )
        g.node.extend([new_node])
        node_to_del.extend([node, pre_node])

        pre_val_info = helper.find_value_by_name(g, node.input[0])
        next_val_info = helper.find_value_by_name(g, node.output[0])
        g.value_info.remove(pre_val_info)
        g.value_info.remove(next_val_info)

        new_val_info = onnx.helper.make_tensor_value_info(
            node.output[0],
            pre_node.attribute[0].t.data_type,
            new_shape
        )
        g.value_info.extend([new_val_info])
        folded = True

    while node_to_del:
        node = node_to_del.pop()
        g.node.remove(node)

    topological_sort(g)
    return folded


def gather_constant_folding(g):
    """Fold constant and gather nodes to a single constant node.
    """
    node_to_del = []
    folded = False
    for node in g.node:
        if node.op_type != 'Gather':
            continue
        pre_data_node = helper.find_node_by_output_name(g, node.input[0])
        pre_indices_node = helper.find_node_by_output_name(g, node.input[1])
        if pre_data_node.op_type != 'Constant' or\
                pre_indices_node.op_type != 'Constant':
            continue

        shape, data = helper.constant_to_list(pre_data_node)
        indice_shape, indices = helper.constant_to_list(pre_indices_node)
        if type(indice_shape) == int:
            indices = indices[0]

        np_data = np.reshape(data, shape)
        axis = node.attribute[0].i

        new_data = np.take(np_data, indices, axis=axis)
        new_shape = new_data.shape
        new_node = helper.list_to_constant(
            node.output[0],
            new_shape,
            new_data.flatten().tolist(),
            data_type=pre_data_node.attribute[0].t.data_type
        )

        node_to_del.extend([node, pre_data_node, pre_indices_node])
        g.node.extend([new_node])

        val_info_1 = helper.find_value_by_name(g, node.input[0])
        val_info_2 = helper.find_value_by_name(g, node.input[1])
        val_info_3 = helper.find_value_by_name(g, node.output[0])
        new_val_info = onnx.helper.make_tensor_value_info(
            new_node.output[0],
            pre_data_node.attribute[0].t.data_type,
            new_shape
        )

        g.value_info.remove(val_info_1)
        g.value_info.remove(val_info_2)
        g.value_info.remove(val_info_3)
        g.value_info.extend([new_val_info])
        folded = True

    while node_to_del:
        node = node_to_del.pop()
        g.node.remove(node)

    topological_sort(g)
    return folded


def add_constant_folding(g):
    """Fold constant and add nodes to a single constant node.
    """
    node_to_del = []
    folded = False
    for node in g.node:
        if node.op_type != 'Add':
            continue
        pre_node_1 = helper.find_node_by_output_name(g, node.input[0])
        pre_node_2 = helper.find_node_by_output_name(g, node.input[1])
        if not pre_node_1 or not pre_node_2:
            continue
        if pre_node_1.op_type != 'Constant' or \
                pre_node_2.op_type != 'Constant':
            continue
        if len(helper.find_nodes_by_input_name(g, pre_node_1.output[0])) != 1:
            continue
        if len(helper.find_nodes_by_input_name(g, pre_node_2.output[0])) != 1:
            continue

        shape1, data1 = helper.constant_to_list(pre_node_1)
        shape2, data2 = helper.constant_to_list(pre_node_2)
        np_data1 = np.reshape(data1, shape1)
        np_data2 = np.reshape(data2, shape2)
        try:
            new_data = np.add(np_data1, np_data2)
        except:
            raise RuntimeError('can\'t broadcast and add two data sets')

        new_node = helper.list_to_constant(
            node.output[0],
            new_data.shape,
            new_data.flatten().tolist(),
            data_type=pre_node_1.attribute[0].t.data_type
        )

        g.node.extend([new_node])
        node_to_del.extend([node, pre_node_1, pre_node_2])
        g.value_info.remove(helper.find_value_by_name(g, pre_node_1.output[0]))
        g.value_info.remove(helper.find_value_by_name(g, pre_node_2.output[0]))
        folded = True

    while node_to_del:
        node = node_to_del.pop()
        g.node.remove(node)

    topological_sort(g)
    return folded


def sqrt_constant_folding(g):
    """ Fold constant and sqrt nodes to a single node.
    """
    node_to_del = []
    folded = False
    for node in g.node:
        if node.op_type != 'Sqrt':
            continue
        pre_node = helper.find_node_by_output_name(g, node.input[0])
        if pre_node.op_type != 'Constant':
            continue

        shape, data = helper.constant_to_list(pre_node)
        np_data = np.sqrt(np.reshape(data, shape))
        output_val_info = helper.find_value_by_name(g, node.output[0])
        input_val_info = helper.find_value_by_name(g, node.input[0])
        data_type = output_val_info.type.tensor_type.elem_type

        new_tensor = onnx.helper.make_tensor(
            name=node.output[0]+'_data',
            data_type=data_type,
            dims=shape,
            vals=np_data.flatten().tolist()
        )
        new_node = onnx.helper.make_node(
            'Constant',
            [],
            [node.output[0]],
            name=node.output[0],
            value=new_tensor
        )

        g.value_info.remove(input_val_info)
        node_to_del.extend([pre_node, node])
        g.node.extend([new_node])
        folded = True

    while node_to_del:
        node = node_to_del.pop()
        g.node.remove(node)

    topological_sort(g)
    return folded


def reciprocal_constant_folding(g):
    """ Fold constant and reciprocal nodes to a single constant node.
    """
    node_to_del = []
    folded = False
    for node in g.node:
        if node.op_type != 'Reciprocal':
            continue
        pre_node = helper.find_node_by_output_name(g, node.input[0])
        if pre_node.op_type != 'Constant':
            continue
        shape, data = helper.constant_to_list(pre_node)
        data = list(map(lambda x: x if abs(x) > 1.e-8 else 1.e-8, data))
        np_data = np.reshape(data, shape)
        np_data = np.reciprocal(np_data)

        input_val_info = helper.find_value_by_name(g, node.input[0])
        output_val_info = helper.find_value_by_name(g, node.output[0])
        data_type = output_val_info.type.tensor_type.elem_type

        new_tensor = onnx.helper.make_tensor(
            name=node.output[0]+'_data',
            data_type=data_type,
            dims=shape,
            vals=np_data.flatten().tolist()
        )
        new_node = onnx.helper.make_node(
            'Constant',
            [],
            [node.output[0]],
            name=node.output[0],
            value=new_tensor
        )

        node_to_del.extend([node, pre_node])
        g.node.extend([new_node])

        g.value_info.remove(input_val_info)
        folded = True

    while node_to_del:
        node = node_to_del.pop()
        g.node.remove(node)

    topological_sort(g)
    return folded


def mul_constant_folding(g):
    """ Fold constant and mul nodes to a single constant node.
    """
    node_to_del = []
    folded = False
    for node in g.node:
        if node.op_type != 'Mul':
            continue
        pre_node_1 = helper.find_node_by_output_name(g, node.input[0])
        pre_node_2 = helper.find_node_by_output_name(g, node.input[1])
        if pre_node_1.op_type != 'Constant' or pre_node_2.op_type != 'Constant':
            continue

        pre_value_info1 = helper.find_value_by_name(g, node.input[0])
        pre_value_info2 = helper.find_value_by_name(g, node.input[1])
        if pre_value_info1 is None or pre_value_info2 is None:
            continue

        if len(helper.find_nodes_by_input_name(g, pre_value_info1.name)) > 1:
            continue
        if len(helper.find_nodes_by_input_name(g, pre_value_info2.name)) > 1:
            continue

        shape1, data1 = helper.constant_to_list(pre_node_1)
        shape2, data2 = helper.constant_to_list(pre_node_2)
        np_data1 = np.reshape(data1, shape1)
        np_data2 = np.reshape(data2, shape2)

        try:
            new_data = np.multiply(np_data1, np_data2)
        except:
            raise RuntimeError('can not broadcast and multiply two data sets')

        # Special shape for single element.
        if shape1 == 1 and shape2 == 1:
            new_shape = []
        else:
            new_shape = new_data.shape

        new_tensor = onnx.helper.make_tensor(
            name=node.output[0]+'_data',
            data_type=pre_node_1.attribute[0].t.data_type,
            dims=new_shape,
            vals=new_data.flatten().tolist()
        )
        new_node = onnx.helper.make_node(
            'Constant',
            [],
            [node.output[0]],
            name=node.output[0],
            value=new_tensor
        )

        node_to_del.extend([node, pre_node_1, pre_node_2])
        g.node.extend([new_node])

        g.value_info.remove(pre_value_info1)
        g.value_info.remove(pre_value_info2)
        folded = True

    while node_to_del:
        node = node_to_del.pop()
        g.node.remove(node)

    topological_sort(g)
    return folded


def div_constant_folding(g):
    """ Fold constant and mul nodes to a single constant node.
    """
    node_to_del = []
    folded = False
    for node in g.node:
        if node.op_type != 'Div':
            continue
        pre_node_1 = helper.find_node_by_output_name(g, node.input[0])
        pre_node_2 = helper.find_node_by_output_name(g, node.input[1])
        if pre_node_1.op_type != 'Constant' or pre_node_2.op_type != 'Constant':
            continue

        pre_value_info1 = helper.find_value_by_name(g, node.input[0])
        pre_value_info2 = helper.find_value_by_name(g, node.input[1])
        if pre_value_info1 is None or pre_value_info2 is None:
            continue

        if len(helper.find_nodes_by_input_name(g, pre_value_info1.name)) > 1:
            continue
        if len(helper.find_nodes_by_input_name(g, pre_value_info2.name)) > 1:
            continue

        shape1, data1 = helper.constant_to_list(pre_node_1)
        shape2, data2 = helper.constant_to_list(pre_node_2)
        np_data1 = np.reshape(data1, shape1)
        np_data2 = np.reshape(data2, shape2)

        try:
            new_data = np.divide(np_data1, np_data2)
        except:
            raise RuntimeError('can not broadcast and multiply two data sets')

        # Special shape for single element.
        if shape1 == 1 and shape2 == 1:
            new_shape = []
        else:
            new_shape = new_data.shape

        new_tensor = onnx.helper.make_tensor(
            name=node.output[0]+'_data',
            data_type=pre_node_1.attribute[0].t.data_type,
            dims=new_shape,
            vals=new_data.flatten().tolist()
        )
        new_node = onnx.helper.make_node(
            'Constant',
            [],
            [node.output[0]],
            name=node.output[0],
            value=new_tensor
        )

        node_to_del.extend([node, pre_node_1, pre_node_2])
        g.node.extend([new_node])

        g.value_info.remove(pre_value_info1)
        g.value_info.remove(pre_value_info2)
        folded = True

    while node_to_del:
        node = node_to_del.pop()
        g.node.remove(node)

    topological_sort(g)
    return folded


def sub_constant_folding(g):
    """ Fold constant and sub nodes to a single node.
    """
    node_to_del = []
    folded = False
    for node in g.node:
        if node.op_type != 'Sub':
            continue
        pre_node_1 = helper.find_node_by_output_name(g, node.input[0])
        pre_node_2 = helper.find_node_by_output_name(g, node.input[1])
        if pre_node_1.op_type != 'Constant':
            continue
        if pre_node_2.op_type != 'Constant':
            continue
        pre_val_info_1 = helper.find_value_by_name(g, node.input[0])
        pre_val_info_2 = helper.find_value_by_name(g, node.input[1])
        if len(helper.find_nodes_by_input_name(g, node.input[0])) > 1:
            continue
        if len(helper.find_nodes_by_input_name(g, node.input[1])) > 1:
            continue

        shape1, data1 = helper.constant_to_list(pre_node_1)
        shape2, data2 = helper.constant_to_list(pre_node_2)

        new_data = np.subtract(data1, data2)
        # Special shape for single element.
        if shape1 == 1 and shape2 == 1:
            new_shape = []
        else:
            new_shape = new_data.shape

        new_tensor = onnx.helper.make_tensor(
            name=node.output[0]+'_data',
            data_type=pre_node_1.attribute[0].t.data_type,
            dims=new_shape,
            vals=helper.flatten_to_list(new_data)
        )
        new_node = onnx.helper.make_node(
            'Constant',
            [],
            [node.output[0]],
            name=node.output[0],
            value=new_tensor
        )

        g.node.extend([new_node])
        node_to_del.extend([node, pre_node_1, pre_node_2])

        g.value_info.remove(pre_val_info_1)
        g.value_info.remove(pre_val_info_2)
        folded = True

    while node_to_del:
        node = node_to_del.pop()
        g.node.remove(node)

    topological_sort(g)
    return folded


def neg_constant_folding(g):
    node_to_del = []
    folded = False
    for node in g.node:
        if node.op_type != 'Neg':
            continue
        pre_node = helper.find_node_by_output_name(g, node.input[0])
        if not pre_node or pre_node.op_type != 'Constant':
            continue

        shape, data_list = helper.constant_to_list(pre_node)
        new_data_list = [-num for num in data_list]

        new_tensor = onnx.helper.make_tensor(
            name=pre_node.name+'_neg_tensor',
            data_type=pre_node.attribute[0].t.data_type,
            dims=shape,
            vals=new_data_list
        )
        new_node = onnx.helper.make_node(
            'Constant',
            [],
            [node.output[0]],
            name=node.output[0],
            value=new_tensor
        )

        g.node.extend([new_node])
        node_to_del.extend([pre_node, node])
        g.value_info.remove(helper.find_value_by_name(g, node.input[0]))
        folded = True

    while node_to_del:
        g.node.remove(node_to_del.pop())

    topological_sort(g)
    return folded


def floor_constant_folding(g):
    node_to_del = []
    folded = False
    for node in g.node:
        if node.op_type != 'Floor':
            continue
        pre_node = helper.find_node_by_output_name(g, node.input[0])
        if not pre_node or pre_node.op_type != 'Constant':
            continue

        shape, data = helper.constant_to_list(pre_node)
        new_data = np.floor(data).flatten().tolist()

        if shape == 1:
            new_shape = []
        else:
            new_shape = shape

        new_tensor = onnx.helper.make_tensor(
            name=node.output[0]+'_data',
            data_type=pre_node.attribute[0].t.data_type,
            dims=new_shape,
            vals=helper.flatten_to_list(new_data)
        )
        new_node = onnx.helper.make_node(
            'Constant',
            [],
            [node.output[0]],
            name=node.output[0],
            value=new_tensor
        )

        g.node.extend([new_node])
        node_to_del.extend([pre_node, node])
        old_value = helper.find_value_by_name(g, node.input[0])
        if old_value is not None:
            g.value_info.remove(old_value)
        folded = True

    while node_to_del:
        g.node.remove(node_to_del.pop())

    topological_sort(g)
    return folded


# Available constant folding names to function map.
constant_folding_nodes = {
    'Add': add_constant_folding,
    'Cast': cast_constant_folding,
    'Concat': concat_constant_folding,
    'Div': div_constant_folding,
    'Floor': floor_constant_folding,
    'Gather': gather_constant_folding,
    'Mul': mul_constant_folding,
    'Reciprocal': reciprocal_constant_folding,
    'ReduceProd': reduceprod_constant_folding,
    'Reshape': reshape_constant_input_folding,
    'Slice': slice_constant_folding,
    'Sqrt': sqrt_constant_folding,
    'Transpose': transpose_constant_folding,
    'Unsqueeze': unsqueeze_constant_folding1,
    'Sub': sub_constant_folding,
    'Neg': neg_constant_folding
}
